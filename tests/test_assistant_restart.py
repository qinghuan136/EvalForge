"""Persist a pending approval across a separate process and a new assistant service."""

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy import func, select

from api_support import APITestCase, SQLiteDatabase
from test_assistant_deferred import frames, streaming_model
from evalforge.db.models import Runner
from evalforge.server.services.assistant import AssistantService


class AssistantRestartTests(APITestCase):
    def make_database(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_url = f"sqlite+aiosqlite:///{Path(temporary.name) / 'assistant.sqlite'}"
        return SQLiteDatabase(self.database_url)

    async def test_pending_approval_survives_a_fresh_process_and_completes_once(self):
        await self.register()
        await self.login()
        service = self.app.state.assistant
        config = replace(self.config, assistant_api_key="unused", assistant_models=("model",))
        service.config = config
        owner = (await self.user()).id
        created = (await self.client.post("/api/v1/assistant/sessions", json={})).json()
        conversation_id = created["conversation_id"]

        def model(messages, _):
            if isinstance(messages[-1].parts[-1], ToolReturnPart):
                return ModelResponse(parts=[TextPart("Runner 已创建")])
            return ModelResponse(parts=[ToolCallPart("RUNNER_create_runner", {"name": "restart"}, tool_call_id="create")])

        with patch("evalforge.server.services.assistant.build_model", return_value=streaming_model(model)):
            paused = await self.client.post("/api/v1/assistant/stream", json={
                "type": "message", "conversation_id": conversation_id, "expected_revision": created["revision"],
                "message": "创建 Runner"})
            state = frames(paused)[-1][1]
            self.assertEqual(state["status"], "awaiting_approval")
            code = """
import asyncio, json, sys
from api_support import SQLiteDatabase
from evalforge.server.assistant.conversation_store import DatabaseConversationStore
async def main():
    async with SQLiteDatabase(sys.argv[1]) as database:
        store = DatabaseConversationStore(database, int(sys.argv[2]))
        conversation = await store.load(sys.argv[3], scope=store.scope)
        print(json.dumps({'revision': conversation.revision, 'calls': [call.tool_call_id for call in conversation.deferred_tool_requests.approvals]}))
asyncio.run(main())
"""
            process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", "-c", code,
                self.database_url, str(owner), conversation_id, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout), {"revision": state["revision"], "calls": ["create"]})
            self.app.state.assistant = AssistantService(self.database, service.experiments, config,
                runners=service.runners, datasets=service.datasets, traces=service.traces)
            request = {"type": "decision", "conversation_id": conversation_id,
                       "expected_revision": state["revision"], "decisions": {"create": True}}
            resumed = await self.client.post("/api/v1/assistant/stream", json=request)
            self.assertEqual(frames(resumed)[-1][1]["status"], "completed", resumed.text)
            self.assertEqual((await self.client.post("/api/v1/assistant/stream", json=request)).status_code, 409)
            history = (await self.client.get(f"/api/v1/assistant/sessions/{conversation_id}")).json()
            self.assertEqual(history["messages"][-1]["text"], "Runner 已创建")
            async with self.database.transaction() as db:
                self.assertEqual(await db.scalar(select(func.count()).select_from(Runner)), 1)
