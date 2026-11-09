import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

from websockets.asyncio.server import serve

from evalforge_client.app import run_client
from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.rpc import JsonRpcPeer
from evalforge_client.session import RunnerSession, SessionError
from evalforge_client.transport import TransportClosed, WebSocketConnection, WebSocketTransport


class FakePeer:
    def __init__(self, replies):
        self.replies, self.calls, self.handlers = replies, [], {}
        self.closed = asyncio.Event()

    def register(self, name, handler):
        self.handlers[name] = handler

    async def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "runner.ready" and method not in self.replies:
            return {"session_id": params["session_id"], "status": "ready"}
        reply = self.replies[method]
        if callable(reply):
            return await reply(params)
        return reply

    async def wait_closed(self):
        await self.closed.wait()


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runner.sqlite3"
        self.config = ClientConfig("wss://cloud.example/runner", "runner-01", "secret", self.path, rpc_timeout=1)

    async def seed(self, repo, attempt="a1", final=False):
        await repo.accept(job_id="j1", attempt_id=attempt, agent_target_id="agent", example_id="e1",
                          input_json={}, timeout_seconds=30)
        await repo.claim("j1", attempt)
        if final:
            await self.finish(repo, attempt)

    async def finish(self, repo, attempt="a1"):
        await repo.finish("j1", attempt, dict(job_id="j1", attempt_id=attempt, status="succeeded",
                                             output={"reply": "好"}, duration_ms=1, http_status=200, error=None))

    async def wait_ready(self, session, task):
        async def wait():
            while not session.ready:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), 3)

    async def test_empty_snapshot_hello_then_sync_and_gate(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            session = RunnerSession(self.config, repo)

            async def hello(params):
                self.assertEqual(params["capabilities"], [])
                self.assertFalse((await peer.handlers["jobs.dispatch"]())["accepted"])
                return {"session_id": "s1", "status": "ready"}

            peer = FakePeer({"runner.hello": hello, "runner.sync": {"jobs": []}})
            task = asyncio.create_task(session.run(peer))
            try:
                await self.wait_ready(session, task)
                self.assertEqual([c[0] for c in peer.calls], ["runner.hello", "runner.sync", "runner.ready"])
                self.assertEqual(peer.calls[1][1]["active_jobs"], [])
                self.assertEqual(await peer.handlers["jobs.dispatch"](),
                                 {"accepted": False, "reason": "executor_unavailable"})
            finally:
                peer.closed.set()
                await task
            self.assertFalse(session.ready)
            self.assertIsNone(session.session_id)

    async def test_ready_response_must_match_session_and_failure_closes_local_gate(self):
        async with ClientDatabase(self.path) as db:
            for ready in ({"session_id": "other", "status": "ready"},
                          {"session_id": "s1", "status": "syncing"}):
                session = RunnerSession(self.config, AttemptRepository(db))
                peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                                 "runner.sync": {"jobs": []}, "runner.ready": ready})
                with self.subTest(ready=ready), self.assertRaises(SessionError):
                    await session.run(peer)
                self.assertFalse(session.ready)
                self.assertEqual(peer.calls[-1], ("runner.ready", {"session_id": "s1"}))

    async def test_historical_pending_requires_cloud_instruction_and_ack(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.seed(repo, final=True)
            session = RunnerSession(self.config, repo)

            async def sync(params):
                self.assertEqual(len(params["pending_results"]), 1)
                self.assertEqual(len(peer.calls), 2)  # 此前没有擅自上传。
                return {"jobs": [dict(job_id="j1", attempt_id="a1", action="retry_upload")]}

            peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                             "runner.sync": sync, "jobs.complete": {"acknowledged": True}})
            task = asyncio.create_task(session.run(peer))
            try:
                await self.wait_ready(session, task)
                self.assertEqual((await repo.get("j1", "a1")).delivery_status, "acknowledged")
                self.assertEqual([c[0] for c in peer.calls], ["runner.hello", "runner.sync", "jobs.complete", "runner.ready"])
            finally:
                peer.closed.set()
                await task

    async def test_sync_completion_race_uploads_only_after_ready_without_reexecution(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.seed(repo)
            jobs = type("Jobs", (), {"capabilities": ["http_json"], "resume": AsyncMock(),
                                     "cancel": AsyncMock(), "dispatch": AsyncMock()})()
            session = RunnerSession(self.config, repo, jobs)
            uploaded = asyncio.Event()

            async def sync(params):
                self.assertEqual(params["active_jobs"][0]["status"], "running")
                await self.finish(repo)
                return {"jobs": [dict(job_id="j1", attempt_id="a1", action="continue")]}

            async def complete(params):
                self.assertTrue(session.ready)
                uploaded.set()
                return {"acknowledged": True}

            peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                             "runner.sync": sync, "jobs.complete": complete})
            task = asyncio.create_task(session.run(peer))
            try:
                await asyncio.wait_for(uploaded.wait(), 3)
                await self.wait_ready(session, task)
                async def wait_ack():
                    while (await repo.get("j1", "a1")).delivery_status != "acknowledged":
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(wait_ack(), 3)
            finally:
                peer.closed.set()
                await task
            self.assertEqual((await repo.get("j1", "a1")).delivery_status, "acknowledged")
            self.assertFalse(await repo.claim("j1", "a1"))
            jobs.resume.assert_not_awaited()
            jobs.cancel.assert_not_awaited()

    async def test_invalid_hello_and_handshake_timeout_leave_gate_closed(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            for hello in ({}, {"session_id": "", "status": "ready"},
                          {"session_id": "s", "status": "denied"}):
                peer = FakePeer({"runner.hello": hello})
                session = RunnerSession(self.config, repo)
                with self.assertRaises(SessionError):
                    await session.run(peer)
                self.assertFalse(session.ready)
                self.assertIsNone(session.session_id)
                self.assertEqual(len(peer.calls), 1)
            async def timeout(params):
                raise TimeoutError()
            peer = FakePeer({"runner.hello": timeout})
            session = RunnerSession(self.config, repo)
            with self.assertRaises(TimeoutError):
                await session.run(peer)
            self.assertFalse(session.ready)

    async def test_invalid_or_missing_sync_actions_never_upload_or_open_gate(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.seed(repo, final=True)
            for jobs in ([], [dict(job_id="j1", attempt_id="a1", action="continue")],
                         [dict(job_id="j1", attempt_id="a1", action="discard")],
                         [dict(job_id="other", attempt_id="a1", action="retry_upload")]):
                peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"}, "runner.sync": {"jobs": jobs}})
                session = RunnerSession(self.config, repo)
                with self.subTest(jobs=jobs), self.assertRaises(SessionError):
                    await session.run(peer)
                self.assertFalse(session.ready)
                self.assertEqual(len(peer.calls), 2)
            self.assertEqual((await repo.get("j1", "a1")).delivery_status, "pending")

    async def test_unacknowledged_upload_and_timeout_preserve_result(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.seed(repo, final=True)
            for response in ({"acknowledged": False}, {"acknowledged": 1}):
                peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                                 "runner.sync": {"jobs": [dict(job_id="j1", attempt_id="a1", action="retry_upload")]},
                                 "jobs.complete": response})
                with self.assertRaises(SessionError):
                    await RunnerSession(self.config, repo).run(peer)
            async def timeout(params):
                raise TimeoutError()
            peer.replies["jobs.complete"] = timeout
            with self.assertRaises(TimeoutError):
                await RunnerSession(self.config, repo).run(peer)
            self.assertEqual((await repo.get("j1", "a1")).delivery_status, "pending")

    async def test_unknown_is_not_in_active_snapshot(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.seed(repo)
            await repo.recover_interrupted()
            session = RunnerSession(self.config, repo)
            peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                             "runner.sync": {"jobs": [dict(job_id="j1", attempt_id="a1", action="continue")]}})
            with self.assertRaises(SessionError):
                await session.run(peer)
            peer.replies["runner.sync"] = {"jobs": []}
            task = asyncio.create_task(session.run(peer))
            try:
                await self.wait_ready(session, task)
                row = await repo.get("j1", "a1")
                self.assertEqual(row.execution_status, "unknown")
                self.assertEqual(peer.calls[-2][1]["active_jobs"], [])
            finally:
                peer.closed.set()
                await task

    async def test_real_socket_app_reconnects_and_recovers_only_once(self):
        async with ClientDatabase(self.path) as db:
            await self.seed(AttemptRepository(db))
        connected_twice = asyncio.Event()
        snapshots = []
        async def handle(socket):
            async with JsonRpcPeer(WebSocketConnection(socket)) as peer:
                async def hello(**params):
                    return {"session_id": f"s{len(snapshots)}", "status": "ready"}
                async def sync(**params):
                    snapshots.append(params)
                    if len(snapshots) == 2:
                        connected_twice.set()
                    return {"jobs": []}
                peer.register("runner.hello", hello)
                peer.register("runner.sync", sync)
                async def ready(session_id):
                    return {"session_id": session_id, "status": "ready"}
                peer.register("runner.ready", ready)
                try:
                    if not snapshots:
                        while not snapshots:
                            await asyncio.sleep(0.005)
                        await asyncio.sleep(0.05)
                        await peer.close()
                    else:
                        await peer.wait_closed()
                except TransportClosed:
                    pass
        async with serve(handle, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            transport = WebSocketTransport(url, allow_insecure_localhost=True, reconnect_min=0.01, reconnect_max=0.02)
            with patch.object(AttemptRepository, "recover_interrupted", autospec=True,
                              side_effect=AttemptRepository.recover_interrupted) as recover:
                task = asyncio.create_task(run_client(self.config, transport=transport))
                try:
                    await asyncio.wait_for(connected_twice.wait(), 5)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertEqual(recover.call_count, 1)
        self.assertEqual([s["active_jobs"] for s in snapshots], [[], []])

    def test_configuration_uses_env_and_config_relative_paths_without_exposing_token(self):
        path = Path(self.temp.name) / "client.json"
        values = dict(cloud_url="wss://cloud.example/runner", runner_id="r1", token_env="TEST_RUNNER_TOKEN",
                      state_path="state/runner.sqlite3", targets={"agent": {"url": "http://127.0.0.1:8001/invoke"}})
        path.write_text(json.dumps(values), encoding="utf-8")
        with patch.dict(os.environ, {"TEST_RUNNER_TOKEN": "topsecret"}):
            config = ClientConfig.load(path)
            self.assertEqual(config.state_path, path.parent / "state" / "runner.sqlite3")
            self.assertNotIn("topsecret", repr(config))
            self.assertFalse(config.state_path.exists())
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            ClientConfig.load(path)


if __name__ == "__main__":
    unittest.main()
