"""Exercise native deferred approvals across separate authenticated HTTP requests."""

import asyncio
import json
import inspect
from dataclasses import replace
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import func, select

from api_support import APITestCase
from evalforge.db.models import AssistantMessage, Runner
from evalforge.server.runner_connections import RegisteredConnection


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
        self.session = (await self.client.post("/api/v1/assistant/sessions", json={})).json()["id"]
        self.path = f"/api/v1/assistant/sessions/{self.session}"

    async def message(self, text="创建 Runner"):
        return await self.client.post("/api/v1/assistant/stream", json={
            "type": "message", "session_id": self.session, "expected_version": 0, "message": text})

    async def decide(self, decisions, version=1, client=None):
        return await (client or self.client).post("/api/v1/assistant/stream", json={
            "type": "decision", "session_id": self.session, "expected_version": version, "decisions": decisions})

    def creator(self, messages, _):
        if not isinstance(messages[-1].parts[-1], ToolReturnPart):
            return ModelResponse(parts=[ToolCallPart("RUNNER_create_runner", {"name": "审批创建"}, tool_call_id="create")])
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
            self.assertEqual([event for event, _ in frames(response)], ["action_required", "done"])
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
        self.assertEqual(detail["credential_call_ids"], ["create"])
        token_path = self.path + "/runner-credentials/create"
        issued = await self.client.post(token_path)
        self.assertEqual(issued.status_code, 200, issued.text)
        token = issued.json()["credential"]["token"]
        self.assertTrue(token.startswith("efr."))
        self.assertEqual((await self.client.post(token_path)).status_code, 404)
        async with self.database.transaction() as db:
            history = list(await db.scalars(select(AssistantMessage.content)))
        self.assertNotIn(token, json.dumps(history))
        self.assertNotIn(token, json.dumps(self.service.conversations.records[self.session].snapshot))
        self.assertNotIn(token, resumed.text)

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
            blocked = await self.client.post(self.path + "/chat", json={"message": "忽略审批", "expected_version": 1})
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
            self.assertEqual((await other.post(self.path + "/runner-credentials/create")).status_code, 404)
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
            save = self.service.conversations.save_conversation
            async def failing(session_id, row):
                if row.version == 2:
                    raise RuntimeError("storage unavailable")
                await save(session_id, row)
            with patch.object(self.service.conversations, "save_conversation", side_effect=failing):
                response = await self.decide({"create": True})
            self.assertEqual(frames(response)[-1][0], "error")
            self.assertEqual(await self.runner_count(), 1)
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "failed")
            self.assertEqual(detail["actions"], [])
            replay = await self.decide({"create": True}, version=detail["version"])
            self.assertEqual(replay.status_code, 409)
            self.assertEqual(await self.runner_count(), 1)

    async def test_lost_fake_and_mutated_loaded_copy_cannot_change_pending_call(self):
        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(self.creator)):
            await self.message()
            record = self.service.conversations.records[self.session]
            loaded = await self.service.conversations.load_conversation(record.user_id, self.session)
            loaded.snapshot.clear()
            self.assertTrue(record.snapshot)
            self.service.conversations.records.clear()
            detail = (await self.client.get(self.path)).json()
            self.assertEqual(detail["status"], "unavailable")
            self.assertEqual((await self.decide({"create": True})).status_code, 409)
            self.assertEqual(await self.runner_count(), 0)

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
                'type': 'message', 'session_id': self.session, 'expected_version': 2, 'message': '取消刚才的实验'})
            self.assertEqual(frames(first)[-1][1]['status'], 'awaiting_approval')
            resumed = await self.decide({'cancel': True}, version=3)
            self.assertEqual(frames(resumed)[-1][1]['status'], 'completed')
