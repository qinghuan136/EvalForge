"""助手的一轮对话、隔离和租约；用 FunctionModel 跑真实 Pydantic AI 工具循环。"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import func, select, update

from api_support import APITestCase
from evalforge.db.models import AssistantMessage, AssistantSession, Dataset, DatasetExample, DatasetVersion, Experiment, Job, Runner
from evalforge.server.schemas.page_context import PageContextCatalog

SESSIONS = "/api/v1/assistant/sessions"
SSE = {"Accept": "text/event-stream"}


class AssistantAPITests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()
        self.user_id = (await self.user()).id
        async with self.database.transaction() as db:
            dataset = Dataset(name="助手数据集", user_id=self.user_id)
            version = DatasetVersion(dataset=dataset)
            experiment = Experiment(name="问答实验", dataset=dataset, dataset_version=version,
                                    user_id=self.user_id, repetitions=1, status="running")
            db.add(experiment)
            await db.flush()
            self.experiment_id = experiment.id
        self.service = self.app.state.assistant
        self.service.config = replace(self.config, assistant_api_key="unused", assistant_models=("model-a", "model-b"))
        response = await self.client.post(SESSIONS, json={"title": "实验会话"})
        self.assertEqual(response.status_code, 201, response.text)
        self.session_id = response.json()["id"]
        self.path = f"{SESSIONS}/{self.session_id}"

    async def chat(self, version=0, text="跑到哪里了？", model="model-a", **overrides):
        return await self.client.post(self.path + "/chat", json=self.chat_body(version, text, model, **overrides))

    def chat_body(self, version=0, text="跑到哪里了？", model="model-a", **overrides):
        return {
            "experiment_id": self.experiment_id, "expected_version": version,
            "message": text, "model_name": model, **overrides,
        }

    async def open_asgi_stream(self, received: asyncio.Queue, disconnected: asyncio.Event):
        body = self.chat_body(experiment_id=None, page_context={"type": "experiment", "id": self.experiment_id})
        request = self.client.build_request("POST", self.path + "/chat", headers=SSE, json=body)
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": request.method, "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path, "raw_path": request.url.raw_path, "query_string": b"",
            "server": (request.url.host, request.url.port), "client": ("127.0.0.1", 1234), "root_path": "",
        }
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": request.content}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        # HTTPX ASGITransport buffers the entire response, so observe raw ASGI frames here.
        await self.app(scope, receive, received.put)

    async def test_real_tool_calls_round_trip_in_history_and_allow_model_switch(self):
        seen_history = []

        def build_model(name, *_):
            def respond(messages, info):
                tail = messages[-1].parts[-1]
                if not isinstance(tail, ToolReturnPart):
                    seen_history.append([part.tool_call_id for message in messages for part in message.parts
                                         if isinstance(part, ToolReturnPart)])
                    self.assertEqual([tool.name for tool in info.function_tools], ["get_experiment_progress"])
                    return ModelResponse(parts=[ToolCallPart("get_experiment_progress", {}, tool_call_id=name)])
                self.assertEqual(tail.content["experiment_id"], self.experiment_id)
                self.assertEqual(tail.content["status"], "running")
                return ModelResponse(parts=[TextPart("实验正在执行")], provider_name="test-provider")
            return FunctionModel(respond, model_name=f"{name}-resolved")

        with patch("evalforge.server.services.assistant.build_model", side_effect=build_model):
            first = await self.chat()
            self.assertEqual(first.status_code, 200, first.text)
            blocked = await self.chat()
            self.assertEqual(blocked.json()["error"]["code"], "assistant_history_changed")
            second = await self.chat(version=1, text="再核对一下。", model="model-b")
            self.assertEqual(second.status_code, 200, second.text)

        self.assertEqual(seen_history, [[], ["model-a"]])
        detail = await self.client.get(self.path)
        self.assertEqual(detail.headers.get("cache-control"), "no-store")
        self.assertEqual(detail.json()["version"], 2)
        visible = detail.json()["messages"]
        self.assertEqual([m["role"] for m in visible], ["user", "assistant", "user", "assistant"])
        self.assertEqual([m["model_name"] for m in visible], [None, "model-a-resolved", None, "model-b-resolved"])
        self.assertEqual(visible[-1]["source_experiment_id"], self.experiment_id)
        async with self.database.transaction() as db:
            rows = (await db.scalars(select(AssistantMessage).order_by(AssistantMessage.id))).all()
            self.assertEqual([m.role for m in rows], ["user", "assistant", "tool", "assistant"] * 2)
            self.assertEqual([m.content["v"] for m in rows], [1] * 8)

    async def test_auth_csrf_owned_experiment_and_model_allowlist(self):
        self.assertEqual((await self.new_client().get(self.path)).status_code, 401)
        other = self.new_client()
        await self.register("bob", other)
        await self.login(other, username="bob")
        self.assertEqual((await other.get(self.path)).status_code, 404)
        self.assertEqual((await other.get(SESSIONS)).json()["items"], [])
        their_session = (await other.post(SESSIONS, json={})).json()["id"]
        payload = {"experiment_id": self.experiment_id, "expected_version": 0, "message": "hello"}
        self.assertEqual((await other.post(self.path + "/chat", json=payload)).status_code, 404)
        forbidden_reference = await other.post(f"{SESSIONS}/{their_session}/chat", json=payload)
        self.assertEqual(forbidden_reference.json()["error"]["code"], "experiment_not_found")
        self.assertEqual((await self.chat(model="unlisted")).status_code, 422)
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual((await self.chat()).status_code, 403)

    async def page_contexts(self) -> tuple[list[dict], list[tuple[dict, str]]]:
        async with self.database.transaction() as db:
            experiment = await db.get(Experiment, self.experiment_id)
            other_dataset = Dataset(name="另一数据集", user_id=self.user_id)
            other_version = DatasetVersion(dataset=other_dataset)
            other_experiment = Experiment(
                name="另一实验", dataset=other_dataset, dataset_version=other_version,
                user_id=self.user_id, repetitions=1,
            )
            example = DatasetExample(dataset_id=experiment.dataset_id)
            job = Job(experiment=experiment, dataset_example=example, repetition_number=1)
            runner = Runner(owner_user_id=self.user_id, name="本地 Runner")
            deleted = Runner(owner_user_id=self.user_id, name="已删 Runner", enabled=False,
                             deleted_at=datetime.now(timezone.utc))
            db.add_all([other_experiment, job, runner, deleted])
            await db.flush()
            contexts = [
                {"type": "experiment", "id": experiment.id},
                {"type": "job", "experiment_id": experiment.id, "job_id": job.job_id},
                {"type": "dataset", "id": experiment.dataset_id, "version_id": experiment.dataset_version_id},
                {"type": "runner", "id": runner.runner_id},
            ]
            mismatches = [
                ({**contexts[1], "experiment_id": other_experiment.id}, "job_not_found"),
                ({**contexts[2], "version_id": other_version.id}, "dataset_version_not_found"),
                ({"type": "runner", "id": deleted.runner_id}, "runner_not_found"),
            ]
        return contexts, mismatches

    async def test_typed_page_context_is_saved_and_replayed_per_user_message(self):
        contexts, _ = await self.page_contexts()
        self.assertEqual(set(PageContextCatalog.list_types()), {context["type"] for context in contexts})
        seen = []

        def respond(messages, info):
            seen.append((
                [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)],
                [tool.name for tool in info.function_tools],
            ))
            return ModelResponse(parts=[TextPart("收到")])

        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(respond)):
            for version, context in enumerate([*contexts, None]):
                body = {"expected_version": version, "message": f"问题 {version}"}
                if context is not None:
                    body["page_context"] = context
                response = await self.client.post(self.path + "/chat", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                user_message, assistant_message = response.json()["messages"]
                self.assertEqual(user_message["text"], f"问题 {version}")
                self.assertEqual(user_message["page_context"], context)
                self.assertIsNone(assistant_message["page_context"])

        self.assertEqual(seen[0][1], ["get_experiment_progress"])
        self.assertTrue(all(not tools for _, tools in seen[1:]))
        all_prompts = seen[-1][0]
        for index, context in enumerate(contexts):
            block, text = all_prompts[index].split("\n\n", 1)
            self.assertEqual(json.loads(block.removeprefix("<page_context>").removesuffix("</page_context>")), context)
            self.assertEqual(text, f"问题 {index}")
        self.assertEqual(all_prompts[-1], "问题 4")
        saved = (await self.client.get(self.path)).json()["messages"]
        self.assertEqual([m["page_context"] for m in saved if m["role"] == "user"], [*contexts, None])
        async with self.database.transaction() as db:
            rows = (await db.scalars(select(AssistantMessage).where(AssistantMessage.role == "user"))).all()
            for row in rows:
                part = next(p for p in row.content["message"]["parts"] if p["part_kind"] == "user-prompt")
                self.assertEqual(part["content"], row.content["text"])

    async def test_context_requests_reject_invalid_shape_parent_or_owner_before_sse_starts(self):
        contexts, mismatches = await self.page_contexts()
        body = {"expected_version": 0, "message": "检查上下文"}
        other = self.new_client()
        await self.register("bob", other)
        await self.login(other, username="bob")
        their_session = (await other.post(SESSIONS, json={})).json()["id"]
        their_url = f"{SESSIONS}/{their_session}/chat"
        ours = self.path + "/chat"
        with patch("evalforge.server.services.assistant.build_model") as build:
            for context, code in zip(contexts, ("experiment", "job", "dataset", "runner"), strict=True):
                response = await other.post(their_url, json={**body, "page_context": context}, headers=SSE)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], f"{code}_not_found")
            for context, code in mismatches:
                response = await self.client.post(ours, json={**body, "page_context": context}, headers=SSE)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
            for invalid in (
                {"page_context": {"type": "trace", "id": 1}},
                {"page_context": {**contexts[0], "owner_id": self.user_id}},
                {"page_context": {"type": "job", "job_id": "job-without-parent"}},
                {"page_context": contexts[1], "experiment_id": self.experiment_id},
            ):
                self.assertEqual((await self.client.post(ours, json={**body, **invalid})).status_code, 422)
            build.assert_not_called()
        detail = (await self.client.get(self.path)).json()
        self.assertEqual((detail["version"], detail["messages"]), (0, []))

    async def test_busy_request_and_late_holder_cannot_save_or_clear_new_lease(self):
        started, reply = asyncio.Event(), asyncio.Event()

        async def hold(*_):
            started.set()
            await reply.wait()
            return ModelResponse(parts=[TextPart("旧执行")])

        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(hold)):
            task = asyncio.create_task(self.chat())
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual((await self.chat()).json()["error"]["code"], "assistant_session_busy")
                successor = uuid4()
                async with self.database.transaction() as db:
                    await db.execute(update(AssistantSession).where(AssistantSession.id == self.session_id).values(
                        active_run_id=successor, lease_expires_at=func.datetime("now", "+30 seconds"),
                    ))
                reply.set()
                self.assertEqual((await asyncio.wait_for(task, 5)).json()["error"]["code"], "assistant_run_expired")
            finally:
                reply.set()
                await asyncio.gather(task, return_exceptions=True)
        async with self.database.transaction() as db:
            row = await db.get(AssistantSession, self.session_id)
            self.assertEqual((row.active_run_id, row.version), (successor, 0))
            self.assertEqual(await db.scalar(select(func.count()).select_from(AssistantMessage)), 0)
            await db.execute(update(AssistantSession).where(AssistantSession.id == self.session_id)
                             .values(lease_expires_at=func.datetime("now", "-2 seconds")))
        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(
            lambda *_: ModelResponse(parts=[TextPart("接管成功")])
        )):
            taken_over = await self.chat()
        self.assertEqual(taken_over.status_code, 200, taken_over.text)
        self.assertEqual(taken_over.json()["version"], 1)

    async def test_model_timeout_releases_lease_without_committing_partial_turn(self):
        self.service.config = replace(self.service.config, assistant_timeout_seconds=1)

        async def hold(*_):
            await asyncio.Event().wait()

        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(hold)):
            response = await self.chat()
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual((await self.client.get(self.path)).json()["messages"], [])
        async with self.database.transaction() as db:
            row = await db.get(AssistantSession, self.session_id)
            self.assertEqual((row.active_run_id, row.lease_expires_at, row.version), (None, None, 0))

    async def test_sse_streams_tool_answer_before_commit_and_returns_saved_turn(self):
        continue_model = asyncio.Event()

        async def model(messages, _):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                yield {0: DeltaToolCall(name="get_experiment_progress", json_args="{}", tool_call_id="progress")}
                return
            self.assertEqual(messages[-1].parts[-1].content["experiment_id"], self.experiment_id)
            yield "实验"
            await continue_model.wait()
            yield "正在执行"

        frames, disconnected = asyncio.Queue(), asyncio.Event()
        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(stream_function=model)):
            task = asyncio.create_task(self.open_asgi_stream(frames, disconnected))
            try:
                start = await asyncio.wait_for(frames.get(), 5)
                self.assertEqual(start["status"], 200)
                headers = dict(start["headers"])
                self.assertTrue(headers[b"content-type"].startswith(b"text/event-stream"))
                self.assertEqual(headers[b"x-accel-buffering"], b"no")
                first = await asyncio.wait_for(frames.get(), 5)
                self.assertIn("event: text_delta\ndata: {\"text\":\"实验\"}", first["body"].decode())
                detail = await self.client.get(self.path)
                self.assertEqual((detail.json()["version"], detail.json()["messages"]), (0, []))
                conflicting = await self.client.post(self.path + "/chat", json=self.chat_body(), headers=SSE)
                self.assertEqual(conflicting.status_code, 409)
                self.assertEqual(conflicting.json()["error"]["code"], "assistant_session_busy")
                continue_model.set()
                second = await asyncio.wait_for(frames.get(), 5)
                self.assertIn("正在执行", second["body"].decode())
                completed = (await asyncio.wait_for(frames.get(), 5))["body"].decode()
                self.assertTrue(completed.startswith("event: done\n"))
                result = json.loads(completed.splitlines()[1].removeprefix("data: "))
                detail = (await self.client.get(self.path)).json()
                self.assertEqual(result, {"version": 1, "messages": detail["messages"]})
                self.assertEqual(result["messages"][-1]["source_experiment_id"], self.experiment_id)
                self.assertEqual(result["messages"][-1]["text"], "实验正在执行")
                await asyncio.wait_for(task, 5)
            finally:
                continue_model.set()
                disconnected.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_sse_disconnect_cancels_generation_and_releases_lease(self):
        stopped = asyncio.Event()

        async def model(*_):
            try:
                yield "部分回答"
                await asyncio.Event().wait()
            finally:
                stopped.set()

        frames, disconnected = asyncio.Queue(), asyncio.Event()
        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(stream_function=model)):
            task = asyncio.create_task(self.open_asgi_stream(frames, disconnected))
            try:
                self.assertEqual((await asyncio.wait_for(frames.get(), 5))["status"], 200)
                self.assertIn(b"event: text_delta", (await asyncio.wait_for(frames.get(), 5))["body"])
                disconnected.set()
                await asyncio.wait_for(task, 5)
                self.assertTrue(stopped.is_set())
            finally:
                disconnected.set()
                await asyncio.gather(task, return_exceptions=True)

        async with self.database.transaction() as db:
            row = await db.get(AssistantSession, self.session_id)
            self.assertEqual((row.active_run_id, row.lease_expires_at, row.version), (None, None, 0))
            self.assertEqual(await db.scalar(select(func.count()).select_from(AssistantMessage)), 0)

    async def test_sse_errors_use_http_before_start_and_event_after_start(self):
        rejected = await self.client.post(self.path + "/chat", json=self.chat_body(model="other"), headers=SSE)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["error"]["code"], "assistant_model_unsupported")

        async def model(*_):
            yield "未保存"
            raise RuntimeError("private provider details")

        with patch("evalforge.server.services.assistant.build_model", return_value=FunctionModel(stream_function=model)):
            failed = await self.client.post(self.path + "/chat", json=self.chat_body(), headers=SSE)
        self.assertEqual(failed.status_code, 200)
        self.assertIn("event: text_delta", failed.text)
        self.assertIn('event: error\ndata: {"code":"assistant_model_failed"', failed.text)
        self.assertNotIn("private provider details", failed.text)
        self.assertNotIn("event: done", failed.text)
        self.assertEqual((await self.client.get(self.path)).json()["messages"], [])
