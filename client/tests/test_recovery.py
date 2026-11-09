import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from websockets.asyncio.server import serve

from evalforge_client.app import run_client
from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.job_manager import JobManager
from evalforge_client.rpc import JsonRpcPeer
from evalforge_client.session import RunnerSession
from evalforge_client.transport import TransportClosed, WebSocketConnection, WebSocketTransport

from test_cancellation import WaitingBody
from test_job_lifecycle import dispatch_params, wait_until
from test_session import FakePeer


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runner.sqlite3"
        self.requests = []
        self.body = None

        async def agent(request):
            self.requests.append(request)
            if self.body is not None:
                return httpx.Response(200, stream=self.body)
            return httpx.Response(200, json={"answer": "saved"})

        self.transport = httpx.MockTransport(agent)

    async def components(self, stack, path=None):
        db = await stack.enter_async_context(ClientDatabase(path or self.path))
        repo = AttemptRepository(db)
        await repo.recover_interrupted()
        executor = await stack.enter_async_context(HttpExecutor(
            {"agent": {"url": "http://agent.example/invoke"}}, transport=self.transport,
        ))
        jobs = await stack.enter_async_context(JobManager(repo, executor))
        return repo, jobs

    def peer(self, sync=None, complete=None):
        return FakePeer({"runner.hello": {"session_id": "s", "status": "ready"},
                         "runner.sync": {"jobs": []} if sync is None else sync,
                         "jobs.started": {"acknowledged": True},
                         "jobs.complete": {"acknowledged": True} if complete is None else complete})

    async def start_session(self, repo, jobs, peer):
        config = ClientConfig("wss://cloud.example/runner", "runner", "token", self.path)
        session = RunnerSession(config, repo, jobs)
        task = asyncio.create_task(session.run(peer))

        async def cleanup():
            peer.closed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        async def ready():
            if task.done():
                await task
            return session.ready
        await wait_until(ready)
        return session, task

    async def acknowledged(self, repo):
        async def ack():
            row = await repo.get("j1", "a1")
            return row is not None and row.delivery_status == "acknowledged"
        await wait_until(ack)

    async def seed(self, path, *, running=False, cancel=False):
        async with ClientDatabase(path) as db:
            repo = AttemptRepository(db)
            await repo.accept(job_id="j1", attempt_id="a1", agent_target_id="agent", example_id="e1",
                              input_json={"question": "你好"}, timeout_seconds=30)
            if running:
                await repo.claim("j1", "a1")
            if cancel:
                await repo.request_cancel("j1", "a1", "original_cancel")

    async def test_disconnect_does_not_cancel_http_and_completion_during_sync_is_uploaded(self):
        self.body = WaitingBody()
        async with AsyncExitStack() as stack:
            repo, jobs = await self.components(stack)
            first = self.peer()
            _, task = await self.start_session(repo, jobs, first)
            await first.handlers["jobs.dispatch"](**dispatch_params(timeout_seconds=30))
            await asyncio.wait_for(self.body.entered.wait(), 3)
            first.closed.set()
            await task
            self.assertFalse(self.body.closed.is_set())
            self.assertEqual((await repo.get("j1", "a1")).execution_status, "running")

            async def sync(params):
                self.assertEqual(params["active_jobs"], [{"job_id": "j1", "attempt_id": "a1", "status": "running"}])
                self.body.release.set()
                async def complete():
                    return (await repo.get("j1", "a1")).delivery_status == "pending"
                await wait_until(complete)
                return {"jobs": [{"job_id": "j1", "attempt_id": "a1", "action": "continue"}]}

            second = self.peer(sync=sync)
            _, second_task = await self.start_session(repo, jobs, second)
            await self.acknowledged(repo)
            second.closed.set()
            await second_task
            self.assertEqual(len(self.requests), 1)
            self.assertEqual((await repo.get("j1", "a1")).execution_status, "succeeded")

    async def test_reconnect_sync_cancel_stops_surviving_http_attempt(self):
        self.body = WaitingBody()
        async with AsyncExitStack() as stack:
            repo, jobs = await self.components(stack)
            first = self.peer()
            _, task = await self.start_session(repo, jobs, first)
            await first.handlers["jobs.dispatch"](**dispatch_params(timeout_seconds=30))
            await asyncio.wait_for(self.body.entered.wait(), 3)
            first.closed.set()
            await task
            second = self.peer(sync={"jobs": [{"job_id": "j1", "attempt_id": "a1", "action": "cancel"}]})
            _, task = await self.start_session(repo, jobs, second)
            await self.acknowledged(repo)
            second.closed.set()
            await task
            self.assertTrue(self.body.closed.is_set())
            self.assertEqual((await repo.get("j1", "a1")).execution_status, "cancelled")
            self.assertEqual(len(self.requests), 1)

    async def test_lost_result_ack_survives_process_restart_and_replays_identical_result(self):
        cloud_results = []

        async def lost_ack(params):
            cloud_results.append(params)
            raise TransportClosed("ACK lost after cloud saved result")

        async with AsyncExitStack() as stack:
            repo, jobs = await self.components(stack)
            first = self.peer(complete=lost_ack)
            _, task = await self.start_session(repo, jobs, first)
            await first.handlers["jobs.dispatch"](**dispatch_params())
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(task, 3)
            saved = (await repo.get("j1", "a1")).result_json
            self.assertEqual((await repo.get("j1", "a1")).delivery_status, "pending")

        async def sync(params):
            self.assertEqual(params["active_jobs"], [])
            self.assertEqual(params["pending_results"], [saved])
            return {"jobs": [{"job_id": "j1", "attempt_id": "a1", "action": "retry_upload"}]}

        async def confirmed(params):
            cloud_results.append(params)
            return {"acknowledged": True}

        async with AsyncExitStack() as stack:
            repo, jobs = await self.components(stack)
            second = self.peer(sync=sync, complete=confirmed)
            _, task = await self.start_session(repo, jobs, second)
            await self.acknowledged(repo)
            second.closed.set()
            await task
            self.assertEqual(cloud_results, [saved, saved])
            self.assertEqual(len(self.requests), 1)

    async def test_restart_accepted_waits_for_continue_and_preserves_cancel_intent(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                path = Path(self.temp.name) / f"accepted-{cancel}.sqlite3"
                await self.seed(path, cancel=cancel)
                before = len(self.requests)
                async with AsyncExitStack() as stack:
                    repo, jobs = await self.components(stack, path)
                    self.assertEqual(len(self.requests), before)  # 打开组件不自动执行 accepted。
                    peer = self.peer(sync={"jobs": [{"job_id": "j1", "attempt_id": "a1", "action": "continue"}]})
                    _, task = await self.start_session(repo, jobs, peer)
                    await self.acknowledged(repo)
                    peer.closed.set()
                    await task
                    row = await repo.get("j1", "a1")
                    self.assertEqual(row.execution_status, "cancelled" if cancel else "succeeded")
                    self.assertEqual(len(self.requests) - before, 0 if cancel else 1)
                    if cancel:
                        self.assertEqual(row.cancel_reason, "original_cancel")

    async def test_restart_running_becomes_unknown_even_with_cancel_intent(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                path = Path(self.temp.name) / f"running-{cancel}.sqlite3"
                await self.seed(path, running=True, cancel=cancel)
                async with AsyncExitStack() as stack:
                    repo, jobs = await self.components(stack, path)
                    peer = self.peer()
                    _, task = await self.start_session(repo, jobs, peer)
                    row = await repo.get("j1", "a1")
                    self.assertEqual(row.execution_status, "unknown")
                    self.assertIsNone(row.result_json)
                    self.assertEqual(row.cancel_reason, "original_cancel" if cancel else None)
                    self.assertEqual(peer.calls[1][1]["active_jobs"], [])
                    peer.closed.set()
                    await task
        self.assertEqual(self.requests, [])


class RecoveryNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_reconnect_cancel_and_lost_ack_replays_without_second_agent_call(self):
        agent_entered, socket_closed, replayed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests, results, sessions, agent_tasks = [], [], [], set()

        async def agent(reader, writer):
            task = asyncio.current_task()
            agent_tasks.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                requests.append(header.split(b"\r\n", 1)[0])
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                agent_entered.set()
                await reader.read()
                socket_closed.set()
            finally:
                writer.close()
                await writer.wait_closed()
                agent_tasks.discard(task)

        async def cloud(socket):
            number = len(sessions) + 1
            sessions.append(number)
            async with JsonRpcPeer(WebSocketConnection(socket)) as peer:
                async def hello(**params):
                    return {"session_id": f"s{number}", "status": "ready"}

                async def sync(**params):
                    if number == 1:
                        self.assertEqual(params["active_jobs"], [])
                        return {"jobs": []}
                    if number == 2:
                        self.assertEqual(params["active_jobs"][0]["status"], "running")
                        action = "cancel"
                    else:
                        self.assertEqual(params["pending_results"], [results[0]])
                        action = "retry_upload"
                    return {"jobs": [{"job_id": "j1", "attempt_id": "a1", "action": action}]}

                async def started(**params):
                    return {"acknowledged": True}

                async def complete(**params):
                    self.assertEqual(params["status"], "cancelled")
                    results.append(params)
                    if len(results) == 1:
                        await peer.close()  # 云端已存结果，但在响应 ACK 前断线。
                    else:
                        replayed.set()
                    return {"acknowledged": True}

                async def ready(session_id):
                    return {"session_id": session_id, "status": "ready"}
                for method, handler in (("runner.hello", hello), ("runner.sync", sync), ("runner.ready", ready),
                                        ("jobs.started", started), ("jobs.complete", complete)):
                    peer.register(method, handler)
                try:
                    if number == 1:
                        while not (await peer.call("jobs.dispatch", dispatch_params(timeout_seconds=30)))["accepted"]:
                            await asyncio.sleep(0.01)
                        await asyncio.wait_for(agent_entered.wait(), 3)
                        await peer.close()  # Job 执行中断线，重连不能重新发起 HTTP。
                    else:
                        await peer.wait_closed()
                except TransportClosed:
                    pass

        try:
            with tempfile.TemporaryDirectory() as directory:
                async with await asyncio.start_server(agent, "127.0.0.1", 0) as agent_server:
                    async with serve(cloud, "127.0.0.1", 0) as cloud_server:
                        url = f"ws://127.0.0.1:{cloud_server.sockets[0].getsockname()[1]}"
                        config = ClientConfig(url, "runner", "token", Path(directory) / "runner.sqlite3",
                                              targets={"agent": {"url": f"http://127.0.0.1:{agent_server.sockets[0].getsockname()[1]}/invoke"}},
                                              allow_insecure_localhost=True)
                        transport = WebSocketTransport(url, allow_insecure_localhost=True,
                                                       reconnect_min=0.01, reconnect_max=0.02)
                        task = asyncio.create_task(run_client(config, transport=transport))
                        try:
                            await asyncio.wait_for(replayed.wait(), 8)
                            await asyncio.wait_for(socket_closed.wait(), 3)
                            async with ClientDatabase(config.state_path) as db:
                                repo = AttemptRepository(db)
                                async def ack():
                                    return (await repo.get("j1", "a1")).delivery_status == "acknowledged"
                                await wait_until(ack)
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
            self.assertEqual(requests, [b"POST /invoke HTTP/1.1"])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertEqual(sessions, [1, 2, 3])
        finally:
            for task in list(agent_tasks):
                task.cancel()
            await asyncio.gather(*agent_tasks, return_exceptions=True)
