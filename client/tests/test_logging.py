import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.job_manager import JobManager
from evalforge_client.session import RunnerSession
from evalforge_client.transport import ConnectionFailed, WebSocketTransport

from test_job_lifecycle import dispatch_params, wait_until
from test_session import FakePeer


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_logs_correlate_committed_lifecycle_without_business_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.sqlite3"
            config = ClientConfig("wss://cloud.example/?token=cloud-secret", "r1", "runner-secret", path)
            targets = {"agent": {"url": "http://agent.example/invoke?key=url-secret",
                                 "headers": {"Authorization": "Bearer header-secret"}}}
            async with ClientDatabase(path) as db, HttpExecutor(
                targets, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answer": "response-secret"})),
            ) as executor:
                repo = AttemptRepository(db)
                async with JobManager(repo, executor) as jobs:
                    peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                                     "runner.sync": {"jobs": []}, "jobs.started": {"acknowledged": True},
                                     "jobs.complete": {"acknowledged": True}})
                    session = RunnerSession(config, repo, jobs)
                    with self.assertLogs("evalforge_client", level="DEBUG") as logs:
                        task = asyncio.create_task(session.run(peer))
                        async def ready():
                            if task.done():
                                await task
                            return session.ready
                        async def acknowledged():
                            row = await repo.get("j\n1", "a1")
                            return row is not None and row.delivery_status == "acknowledged"
                        try:
                            await wait_until(ready)
                            await peer.handlers["jobs.dispatch"](**dispatch_params("j\n1", input={"secret": "input-secret"}))
                            await wait_until(acknowledged)
                            row = await repo.get("j\n1", "a1")
                            self.assertEqual(row.result_json["output"], {"answer": "response-secret"})
                            await jobs.cancel(row, "cancel-secret")
                        finally:
                            peer.closed.set()
                            await task
                    messages = [record.getMessage() for record in logs.records]
                    events = ("Job accepted", "Job execution started", "Job result saved", "Result acknowledged")
                    positions = [next(i for i, message in enumerate(messages) if message.startswith(event)) for event in events]
                    self.assertEqual(positions, sorted(positions))
                    for index in positions:
                        self.assertIn("job_id='j\\n1'", messages[index])
                        self.assertIn("attempt_id='a1'", messages[index])
                    output = " ".join(messages)
                    for secret in ("input-secret", "response-secret", "header-secret", "url-secret",
                                   "cloud-secret", "runner-secret", "cancel-secret"):
                        self.assertNotIn(secret, output)
                    self.assertTrue(all("\n" not in message for message in messages))

    async def test_connection_and_http_errors_log_types_and_codes_without_raw_exceptions(self):
        transport = WebSocketTransport("wss://cloud.example/?key=url-secret", bearer_token="header-secret")
        with self.assertLogs("evalforge_client", level="DEBUG") as logs:
            with patch("evalforge_client.transport.connect", AsyncMock(side_effect=OSError("exception-secret"))):
                with self.assertRaises(ConnectionFailed):
                    await transport._connect()
            async def agent(request):
                raise httpx.ConnectError("http-exception-secret")
            async with HttpExecutor({"agent": {"url": "http://agent.example/?key=url-secret"}},
                                    transport=httpx.MockTransport(agent)) as executor:
                outcome = await executor.execute("agent", {"input": "input-secret"}, timeout_seconds=1)
        self.assertEqual(outcome.error["code"], "connection_error")
        output = " ".join(record.getMessage() for record in logs.records)
        self.assertIn("error_type=OSError", output)
        self.assertIn("error_code='connection_error'", output)
        for secret in ("url-secret", "header-secret", "exception-secret", "input-secret"):
            self.assertNotIn(secret, output)

    def test_cli_debug_does_not_enable_dependency_payload_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            state = Path(directory) / "runner.sqlite3"
            path.write_text(json.dumps({"cloud_url": "wss://cloud.example/runner", "runner_id": "r1",
                                        "token_env": "LOG_TEST_TOKEN", "state_path": str(state)}), encoding="utf-8")
            script = (
                "import logging, sys; from evalforge_client.__main__ import main; "
                "sys.argv = ['evalforge_client', '--config', sys.argv[1], '--check-config', '--log-level', 'DEBUG']; "
                "main(); logging.getLogger('evalforge_client.probe').debug('client_debug_visible'); "
                "logging.getLogger('httpx').info('dependency-secret'); "
                "logging.getLogger('websockets.client').debug('dependency-secret'); "
                "logging.getLogger('aiosqlite').debug('dependency-secret'); "
                "logging.getLogger('sqlalchemy.engine.Engine').info('dependency-secret')"
            )
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(path)],
                                    env={**os.environ, "LOG_TEST_TOKEN": "runner-secret"},
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DEBUG evalforge_client.probe client_debug_visible", result.stderr)
            self.assertNotIn("dependency-secret", result.stderr)
            self.assertNotIn("runner-secret", result.stderr)
            self.assertFalse(state.exists())
