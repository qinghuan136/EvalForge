"""Exercise native deferred approvals across separate authenticated HTTP requests."""

import asyncio
import json
import inspect
from dataclasses import replace
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import func, select, update
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from api_support import APITestCase
from evalforge.db.models import AssistantConversationEntry as AssistantMessage, AssistantConversationRecord as AssistantSession, Runner
from evalforge.server.runner_connections import RegisteredConnection
from evalforge.server.assistant.conversation_store import DatabaseConversationStore
from evalforge.server.services.assistant import AssistantService


def frames(response):
    return [(block.splitlines()[0].removeprefix("event: "),
             json.loads(block.splitlines()[1].removeprefix("data: ")))
            for block in response.text.strip().split("\n\n") if block.startswith("event:")]


def streaming_model(respond):
    async def stream(messages, info):
        result = respond(messages, info)
        if inspect.isawaitable(result):
            result = await result
        for index, part in enumerate(result.parts):
            if isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args_as_dict()),
                                           tool_call_id=part.tool_call_id)}
            elif isinstance(part, TextPart):
                yield part.content
    return FunctionModel(stream_function=stream)


class DeferredAssistantTests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()
        self.service = self.app.state.assistant
        self.service.config = replace(self.config, assistant_api_key="unused", assistant_models=("model-a",))
        created = (await self.client.post("/api/v1/assistant/sessions", json={})).json()
        self.session = created["conversation_id"]
        self.revisions = {0: created["revision"]}
        self.path = f"/api/v1/assistant/sessions/{self.session}"

    async def message(self, text="创建 Runner"):
        response = await self.client.post("/api/v1/assistant/stream", json={
            "type": "message", "conversation_id": self.session, "expected_revision": self.revisions[0], "message": text})
        if response.status_code == 200 and frames(response)[-1][0] == "done":
            self.revisions[1] = frames(response)[-1][1]["revision"]
        return response

    async def decide(self, decisions, version=1, client=None):
        revision = self.revisions.get(version, "outdated") if isinstance(version, int) else version
        response = await (client or self.client).post("/api/v1/assistant/stream", json={
            "type": "decision", "conversation_id": self.session, "expected_revision": revision, "decisions": decisions})
        if response.status_code == 200 and frames(response)[-1][0] == "done" and isinstance(version, int):
            self.revisions[version + 1] = frames(response)[-1][1]["revision"]
        return response

    def creator(self, messages, _):
        if not isinstance(messages[-1].parts[-1], ToolReturnPart):
            return ModelResponse(parts=[ToolCallPart("RUNNER_create_runner", {
                "name": "审批创建", "approval_description": "为本地测试创建 Runner。"}, tool_call_id="create")])
        result = messages[-1].parts[-1].content
        self.assertNotIn("credential", result)
        self.assertNotIn("efr.", json.dumps(result))
        return ModelResponse(parts=[TextPart("操作已处理")])

    async def runner_count(self):
        async with self.database.transaction() as db:
            return await db.scalar(select(func.count()).select_from(Runner))

    async def test_approve_restores_agent_executes_once_and_delivers_secret_outside_history(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            response = await self.message()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([event for event, _ in frames(response)], ["approval_required", "done"])
            approval = frames(response)[0][1]
            self.assertEqual(approval["cards"][0]["title"], "批准请求")
            self.assertEqual(approval["cards"][0]["description"], "为本地测试创建 Runner。")
            self.assertEqual(approval["cards"][0]["arguments"], {"name": "审批创建"})
            self.assertEqual(set(frames(response)[-1][1]), {"revision", "status"})
            self.assertEqual(await self.runner_count(), 0)
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["actions"][0]["tool_call_id"], "create")
            resumed = await self.decide({"create": True})
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(frames(resumed)[-1][1]["status"], "completed")
            self.assertEqual(await self.runner_count(), 1)
            self.assertEqual((await self.decide({"create": True})).status_code, 409)
            self.assertEqual((await self.decide({"create": True}, version=2)).status_code, 409)
            self.assertEqual(await self.runner_count(), 1)
        detail = (await self.client.get(self.path)).json()
        display = next(data for event, data in frames(resumed) if event == "display")
        self.assertEqual(display["tool_call_id"], "create")
        secret = next(field for field in display["fields"] if field["label"] == "连接凭证")
        self.assertTrue(secret["sensitive"])
        token = secret["value"]
        self.assertTrue(token.startswith("efr."))
        async with self.database.transaction() as db:
            history = list(await db.scalars(select(AssistantMessage.message)))
        self.assertNotIn(token, json.dumps(history))
        self.assertNotIn(token, json.dumps(detail))
        self.assertEqual([event for event, _ in frames(resumed)].count("display"), 1)

    async def test_complete_decision_set_is_required_and_deny_skips_writes(self):
        def model(messages, _):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart("RUNNER_create_runner", {"name": name}, tool_call_id=name)
                                            for name in ("a", "b")])
            return ModelResponse(parts=[TextPart("已按审批处理")])
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(model)):
            await self.message()
            self.assertEqual((await self.decide({"a": True})).status_code, 422)
            self.assertEqual((await self.decide({"a": True, "b": False, "extra": True})).status_code, 422)
            self.assertEqual((await self.decide({"a": "true", "b": False})).status_code, 422)
            blocked = await self.client.post(self.path + "/chat", json={"message": "忽略审批", "expected_revision": self.revisions[1]})
            self.assertEqual(blocked.json()["error"]["code"], "assistant_approval_required")
            resumed = await self.decide({"a": True, "b": False})
            self.assertEqual(frames(resumed)[-1][1]["status"], "completed")
        self.assertEqual(await self.runner_count(), 1)

    async def test_decision_and_secret_delivery_are_owner_scoped(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            other = self.new_client()
            await self.register("other", other)
            await self.login(other, username="other")
            self.assertEqual((await self.decide({"create": True}, client=other)).status_code, 404)
            await self.decide({"create": True})
            self.client.headers.pop("X-CSRF-Token")
            self.assertEqual((await self.decide({"create": True}, version=2)).status_code, 403)

    async def test_concurrent_restore_is_rejected_before_second_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(messages, info):
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                entered.set()
                await release.wait()
            return self.creator(messages, info)
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(model)):
            await self.message()
            running = asyncio.create_task(self.decide({"create": True}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                duplicate = await self.decide({"create": True})
                self.assertEqual(duplicate.json()["error"]["code"], "assistant_session_busy")
                self.assertEqual(await self.runner_count(), 1)
            finally:
                release.set()
                await running

    async def test_failed_snapshot_save_never_reopens_approved_write(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            with patch.object(DatabaseConversationStore, "save", side_effect=RuntimeError("storage unavailable")):
                response = await self.decide({"create": True})
            self.assertEqual(frames(response)[-1][0], "error")
            self.assertEqual(frames(response)[0][0], "display")
            self.assertEqual(await self.runner_count(), 1)
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "failed")
            self.assertEqual(detail["actions"], [])
            replay = await self.decide({"create": True}, version=detail["revision"])
            self.assertEqual(replay.status_code, 409)
            self.assertEqual(await self.runner_count(), 1)

    async def test_display_is_delivered_before_model_finishes_and_survives_its_failure(self):
        failed, continue_model = asyncio.Event(), asyncio.Event()
        async def model(messages, info):
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                failed.set()
                await continue_model.wait()
                raise RuntimeError("model failed after the tool succeeded")
            return self.creator(messages, info)

        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(model)):
            await self.message()
            request = self.client.build_request("POST", "/api/v1/assistant/stream", json={
                "type": "decision", "conversation_id": self.session, "expected_revision": self.revisions[1],
                "decisions": {"create": True}})
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                "method": "POST", "headers": [(key.lower(), value) for key, value in request.headers.raw],
                "scheme": request.url.scheme, "path": request.url.path, "raw_path": request.url.raw_path,
                "query_string": b"", "server": (request.url.host, request.url.port),
                "client": ("127.0.0.1", 1234), "root_path": ""}
            sent = False
            disconnected, received = asyncio.Event(), asyncio.Queue()
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": request.content}
                await disconnected.wait()
                return {"type": "http.disconnect"}
            task = asyncio.create_task(self.app(scope, receive, received.put))
            try:
                self.assertEqual((await asyncio.wait_for(received.get(), 5))["status"], 200)
                frame = (await asyncio.wait_for(received.get(), 5))["body"].decode()
                self.assertTrue(frame.startswith("event: display\n"), frame)
                card = json.loads(frame.splitlines()[1].removeprefix("data: "))
                token = next(field["value"] for field in card["fields"] if field["sensitive"])
                await asyncio.wait_for(failed.wait(), 5)
                self.assertEqual(await self.runner_count(), 1)
                continue_model.set()
                error = (await asyncio.wait_for(received.get(), 5))["body"].decode()
                self.assertTrue(error.startswith("event: error\n"), error)
                await asyncio.wait_for(task, 5)
                detail = (await self.client.get(self.path)).json()
                self.assertNotIn(token, json.dumps(detail))
                async with self.database.transaction() as db:
                    self.assertNotIn(token, json.dumps(list(await db.scalars(select(AssistantMessage.message)))))
            finally:
                continue_model.set()
                disconnected.set()
                await asyncio.gather(task, return_exceptions=True)
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "failed")
            self.assertEqual(detail["actions"], [])
            replay = await self.decide({"create": True}, version=detail["revision"])
            self.assertEqual(replay.status_code, 409)
            self.assertEqual(await self.runner_count(), 1)

    async def test_new_instance_restores_pending_call_and_saved_history(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            owner = (await self.user()).id
            loaded = await self.service.store(owner).load(self.session)
            loaded.messages.clear()
            self.app.state.assistant = AssistantService(self.database, self.service.experiments, self.service.config,
                runners=self.service.runners, datasets=self.service.datasets, traces=self.service.traces)
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "awaiting_approval")
            self.assertEqual(detail["actions"][0]["tool_call_id"], "create")
            self.assertEqual(await self.runner_count(), 0)
            resumed = await self.decide({"create": True})
            self.assertEqual(frames(resumed)[-1][1]["status"], "completed")
            self.assertEqual(await self.runner_count(), 1)
            self.assertEqual([event for event, _ in frames(resumed)].count("display"), 1)

    async def test_two_instances_cannot_restore_the_same_approval(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(messages, info):
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                entered.set()
                await release.wait()
            return self.creator(messages, info)
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(model)):
            await self.message()
            first = asyncio.create_task(self.decide({"create": True}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.app.state.assistant = AssistantService(self.database, self.service.experiments, self.service.config,
                    runners=self.service.runners, datasets=self.service.datasets, traces=self.service.traces)
                duplicate = await self.decide({"create": True})
                self.assertEqual(duplicate.json()["error"]["code"], "assistant_session_busy")
                self.assertEqual(await self.runner_count(), 1)
            finally:
                release.set()
                await first
            self.assertEqual([event for event, _ in frames(first.result())].count("display"), 1)

    async def test_expired_approval_lease_is_persistently_uncertain(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            async with self.database.transaction() as db:
                await db.execute(update(AssistantSession).where(AssistantSession.conversation_id == self.session).values(
                    status="running", active_run_id=uuid4(), lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10)))
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "failed")
            self.assertEqual(detail["error"]["code"], "assistant_execution_uncertain")
            self.assertEqual(detail["actions"], [])
            replay = await self.decide({"create": True})
            self.assertEqual(replay.json()["error"]["code"], "assistant_execution_uncertain")
            self.assertEqual(await self.runner_count(), 0)

    async def test_entry_and_state_failure_roll_back_as_one_transaction(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            before = await self.service.store((await self.user()).id).load(self.session)
            original = DatabaseConversationStore.save
            async def save_then_lose_lease(store, *args, **kwargs):
                entries = await original(store, *args, **kwargs)
                await store.session.execute(update(AssistantSession).where(
                    AssistantSession.conversation_id == self.session).values(active_run_id=uuid4()))
                return entries
            with patch.object(DatabaseConversationStore, "save", new=save_then_lose_lease):
                result = await self.decide({"create": True})
            self.assertEqual(frames(result)[-1][0], "error")
            restored = await self.service.store((await self.user()).id).load(self.session)
            self.assertEqual(restored.messages, before.messages)
            self.assertEqual(restored.revision, before.revision)
            self.assertEqual((await self.client.get(self.path)).json()["status"], "failed")

    async def test_import_create_cancel_and_delete_tools_call_real_services(self):
        runner = (await self.client.post('/api/v1/runners', json={'name': '用于实验'})).json()['runner']['runner_id']
        deleted = (await self.client.post('/api/v1/runners', json={'name': '准备删除'})).json()['runner']['runner_id']
        content = '{"input":{"question":"hello"}}\n'
        dataset = (await self.client.post('/api/v1/datasets/import', data={'name': '实验数据', 'format': 'jsonl'},
                    files={'file': ('sample.jsonl', content)})).json()
        await self.service.experiments.connections.register(runner, RegisteredConnection(
            session_id='ready', phase='ready', capabilities=['http_json']), 60)
        calls = [
            ToolCallPart('DATASET_import_dataset', {'name': '审批导入', 'format': 'jsonl', 'content': content}, tool_call_id='import'),
            ToolCallPart('EXPERIMENT_create_experiment', {'dataset_id': dataset['dataset_id'],
                'dataset_version_id': dataset['dataset_version_id'], 'name': '审批实验',
                'execution_config': {'runner_id': runner, 'agent_target_id': 'agent'}}, tool_call_id='experiment'),
            ToolCallPart('RUNNER_delete_runner', {'runner_id': deleted}, tool_call_id='delete'),
        ]
        results = {}
        def model(messages, _):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=calls)
            results.update({part.tool_call_id: part.content for part in messages[-1].parts if isinstance(part, ToolReturnPart)})
            return ModelResponse(parts=[TextPart('业务操作已完成')])
        with patch('evalforge.server.services.assistant.build_model', return_value=streaming_model(model)):
            first = await self.message()
            self.assertEqual(frames(first)[-1][1]['status'], 'awaiting_approval')
            resumed = await self.decide({part.tool_call_id: True for part in calls})
            self.assertEqual(frames(resumed)[-1][1]['status'], 'completed', resumed.text)
        self.assertIn('example_count', results['import'], results)
        self.assertEqual(results['import']['example_count'], 1)
        self.assertTrue(results['delete']['deleted'])
        experiment_id = results['experiment']['experiment_id']
        def cancel_model(messages, _):
            if not isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[ToolCallPart('EXPERIMENT_cancel_experiment', {'experiment_id': experiment_id}, tool_call_id='cancel')])
            self.assertEqual(messages[-1].parts[-1].content['status'], 'cancelled')
            return ModelResponse(parts=[TextPart('实验已取消')])
        with patch('evalforge.server.services.assistant.build_model', return_value=streaming_model(cancel_model)):
            first = await self.client.post('/api/v1/assistant/stream', json={
                'type': 'message', 'conversation_id': self.session, 'expected_revision': self.revisions[2], 'message': '取消刚才的实验'})
            self.assertEqual(frames(first)[-1][1]['status'], 'awaiting_approval')
            self.revisions[3] = frames(first)[-1][1]['revision']
            resumed = await self.decide({'cancel': True}, version=3)
            self.assertEqual(frames(resumed)[-1][1]['status'], 'completed')
