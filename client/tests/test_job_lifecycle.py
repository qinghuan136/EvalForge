import asyncio
import json
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from websockets.asyncio.server import serve

from evalforge_client.app import run_client
from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.job_manager import JobManager
from evalforge_client.rpc import JsonRpcPeer, RpcError
from evalforge_client.session import RunnerSession, SessionError
from evalforge_client.transport import TransportClosed, WebSocketConnection


def dispatch_params(job="j1", attempt="a1", **changes):
    return {"job_id": job, "attempt_id": attempt, "agent_target_id": "agent",
            "example_id": "e1", "input": {"question": "你好"}, "timeout_seconds": 1, **changes}


async def wait_until(predicate):
    async def wait():
        while not await predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 5)


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.db = await self.stack.enter_async_context(ClientDatabase(Path(self.temp.name) / "state.sqlite3"))
        self.repo = AttemptRepository(self.db)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.requests = []
        self.reply = httpx.Response(200, json={"answer": "你好", "success": False})

        async def agent(request):
            # 在产生外部调用之前，running 必须已经持久化。
            rows = await self.repo.list_active()
            self.assertEqual([r.execution_status for r in rows], ["running"])
            self.requests.append(request)
            self.entered.set()
            await self.release.wait()
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        self.executor = await self.stack.enter_async_context(HttpExecutor(
            {"agent": {"url": "http://agent.example/invoke"}}, transport=httpx.MockTransport(agent),
        ))
        self.jobs = await self.stack.enter_async_context(JobManager(self.repo, self.executor))

    async def finished(self, job="j1", attempt="a1"):
        async def final():
            row = await self.repo.get(job, attempt)
            return row is not None and row.delivery_status == "pending"
        await wait_until(final)
        return await self.repo.get(job, attempt)

    async def test_accept_deduplicate_busy_conflict_and_release_before_ack(self):
        self.release.clear()
        values = dispatch_params()
        self.assertEqual(await self.jobs.dispatch(values), {"accepted": True})
        self.assertIsNotNone(await self.repo.get("j1", "a1"))
        await asyncio.wait_for(self.entered.wait(), 3)
        self.assertEqual(await self.jobs.dispatch(values), {"accepted": True})
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")), {"accepted": False, "reason": "busy"})
        self.assertIsNone(await self.repo.get("j2", "a1"))
        self.assertEqual(await self.jobs.dispatch(dispatch_params(input={"different": 1})),
                         {"accepted": False, "reason": "attempt_conflict"})
        self.release.set()
        row = await self.finished()
        self.assertEqual(row.execution_status, "succeeded")
        self.assertEqual(row.result_json["output"], {"answer": "你好", "success": False})
        self.assertEqual(json.loads(self.requests[0].content), values["input"])
        self.assertEqual(await self.jobs.dispatch(values), {"accepted": True})
        self.assertEqual(len(self.requests), 1)
        # 等待 ACK 的已完成任务不占 Agent 执行容量。
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")), {"accepted": True})
        await self.finished("j2")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((await self.repo.get("j1", "a1")).delivery_status, "pending")

    async def test_concurrent_dispatches_accept_only_one_without_local_queue(self):
        self.release.clear()
        replies = await asyncio.gather(self.jobs.dispatch(dispatch_params("j1")),
                                       self.jobs.dispatch(dispatch_params("j2")))
        self.assertEqual(sum(reply["accepted"] for reply in replies), 1)
        self.assertIn({"accepted": False, "reason": "busy"}, replies)
        await asyncio.wait_for(self.entered.wait(), 3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(await self.repo.list_active()), 1)
        self.release.set()

    async def test_invalid_params_and_unknown_target_never_accept_or_call_agent(self):
        values = dispatch_params()
        for params in (None, {}, {k: v for k, v in values.items() if k != "input"},
                       {**values, "url": "http://other.example"}, dispatch_params(job_id=" "),
                       dispatch_params(timeout_seconds=True), dispatch_params(timeout_seconds=0),
                       dispatch_params(timeout_seconds=float("inf")), dispatch_params(input={1: "bad"}),
                       dispatch_params(stream_output=1), dispatch_params(stream_output="true")):
            with self.subTest(params=params), self.assertRaises(RpcError) as caught:
                await self.jobs.dispatch(params)
            self.assertEqual(caught.exception.code, -32602)
        self.assertEqual(await self.jobs.dispatch(dispatch_params(agent_target_id="missing")),
                         {"accepted": False, "reason": "target_not_found"})
        self.assertEqual(await self.repo.list_active(), [])
        self.assertEqual(self.requests, [])

    async def test_http_failures_become_persisted_failed_results_without_retry(self):
        for index, (reply, code) in enumerate((
            (httpx.Response(500, json={"detail": "Agent failed"}), "http_error"),
            (httpx.Response(200, text="not JSON"), "invalid_json"),
            (httpx.ConnectError("private endpoint"), "connection_error"),
            (httpx.ReadTimeout("private endpoint"), "timeout"),
        )):
            self.reply = reply
            job = f"j{index}"
            await self.jobs.dispatch(dispatch_params(job))
            row = await self.finished(job)
            self.assertEqual(row.execution_status, "failed")
            self.assertEqual(row.result_json["error"]["code"], code)
            self.assertNotIn("private endpoint", json.dumps(row.result_json))
        self.assertEqual(len(self.requests), 4)

    async def test_unexpected_executor_error_is_sanitized_and_saved(self):
        with patch.object(self.executor, "execute", AsyncMock(side_effect=RuntimeError("secret"))):
            await self.jobs.dispatch(dispatch_params())
            row = await self.finished()
        self.assertEqual(row.execution_status, "failed")
        self.assertEqual(row.result_json["error"]["code"], "executor_error")
        self.assertNotIn("secret", json.dumps(row.result_json))

    async def test_persistence_failure_disables_new_execution(self):
        async def failed():
            return await self.jobs.dispatch(dispatch_params("j2")) == {
                "accepted": False, "reason": "executor_unavailable",
            }
        with patch.object(self.repo, "finish", AsyncMock(side_effect=OSError("disk failed"))):
            with self.assertLogs("evalforge_client.job_manager", level="ERROR"):
                await self.jobs.dispatch(dispatch_params())
                await wait_until(failed)
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")),
                         {"accepted": False, "reason": "executor_unavailable"})
        self.assertEqual((await self.repo.get("j1", "a1")).execution_status, "running")
        self.assertEqual(len(self.requests), 1)

    async def test_unknown_allows_new_jobs_and_is_never_reexecuted(self):
        values = dispatch_params()
        await self.repo.accept(job_id="j1", attempt_id="a1", agent_target_id="agent", example_id="e1",
                               input_json=values["input"], timeout_seconds=1)
        await self.repo.claim("j1", "a1")
        await self.repo.recover_interrupted()
        self.assertEqual(await self.jobs.dispatch(values), {"accepted": True})
        with self.assertRaises(RpcError):
            await self.jobs.resume(await self.repo.get("j1", "a1"))
        self.assertEqual(self.requests, [])
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")), {"accepted": True})
        self.assertEqual((await self.finished("j2")).execution_status, "succeeded")
        self.assertEqual((await self.repo.get("j1", "a1")).execution_status, "unknown")
        self.assertEqual(len(self.requests), 1)

    async def test_continue_of_accepted_attempt_starts_once(self):
        self.release.clear()
        row = await self.repo.accept(job_id="j1", attempt_id="a1", agent_target_id="agent", example_id="e1",
                                     input_json={"question": "你好"}, timeout_seconds=1)
        await self.jobs.resume(row)
        await self.jobs.resume(row)
        await asyncio.wait_for(self.entered.wait(), 3)
        await self.jobs.resume(await self.repo.get("j1", "a1"))
        self.assertEqual(len(self.requests), 1)
        self.release.set()
        await self.finished()

    async def test_shutdown_stops_local_wait_and_preserves_uncertain_execution(self):
        self.release.clear()
        await self.jobs.dispatch(dispatch_params())
        await asyncio.wait_for(self.entered.wait(), 3)
        await self.jobs.__aexit__(None, None, None)
        row = await self.repo.get("j1", "a1")
        self.assertEqual(row.execution_status, "running")
        self.assertIsNone(row.result_json)
        self.assertEqual(await self.repo.recover_interrupted(), 1)
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")),
                         {"accepted": False, "reason": "executor_unavailable"})


class JobSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_ack_failure_never_uploads_complete_or_discards_result(self):
        from test_session import FakePeer

        async def timeout(params):
            raise TimeoutError()

        with tempfile.TemporaryDirectory() as directory:
            for index, reply in enumerate(({"acknowledged": False}, {"acknowledged": 1}, timeout)):
                with self.subTest(reply=reply):
                    config = ClientConfig("wss://cloud.example/runner", "runner", "token",
                                          Path(directory) / f"state{index}.sqlite3")
                    async with ClientDatabase(config.state_path) as db, HttpExecutor(
                        {"agent": {"url": "http://agent.example"}},
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answer": "ok"})),
                    ) as executor:
                        repo = AttemptRepository(db)
                        async with JobManager(repo, executor) as jobs:
                            peer = FakePeer({"runner.hello": {"session_id": "s", "status": "ready"},
                                             "runner.sync": {"jobs": []}, "jobs.started": reply})
                            session = RunnerSession(config, repo, jobs)
                            task = asyncio.create_task(session.run(peer))
                            async def ready():
                                return session.ready
                            try:
                                await wait_until(ready)
                                await peer.handlers["jobs.dispatch"](**dispatch_params())
                                with self.assertRaises((TimeoutError, SessionError)):
                                    await asyncio.wait_for(task, 3)
                                self.assertNotIn("jobs.complete", [name for name, _ in peer.calls])
                                row = await repo.get("j1", "a1")
                                self.assertEqual(row.execution_status, "succeeded")
                                self.assertEqual(row.delivery_status, "pending")
                            finally:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)

    async def test_ready_session_runs_job_reports_started_then_complete_and_ack(self):
        from test_session import FakePeer

        with tempfile.TemporaryDirectory() as directory:
            config = ClientConfig("wss://cloud.example/runner", "runner", "token", Path(directory) / "state.sqlite3")
            async with ClientDatabase(config.state_path) as db, HttpExecutor(
                {"agent": {"url": "http://agent.example"}},
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=None, content=b"null")),
            ) as executor:
                repo = AttemptRepository(db)
                async with JobManager(repo, executor) as jobs:
                    peer = FakePeer({"runner.hello": {"session_id": "s", "status": "ready"},
                                     "runner.sync": {"jobs": []}, "jobs.started": {"acknowledged": True},
                                     "jobs.complete": {"acknowledged": True}})
                    session = RunnerSession(config, repo, jobs)
                    task = asyncio.create_task(session.run(peer))
                    async def ready():
                        return session.ready
                    async def acknowledged():
                        row = await repo.get("j1", "a1")
                        return row is not None and row.delivery_status == "acknowledged"
                    try:
                        await wait_until(ready)
                        self.assertEqual(peer.calls[0][1]["capabilities"], ["http_json", "http_sse"])
                        self.assertEqual(await peer.handlers["jobs.dispatch"](**dispatch_params(input=None)), {"accepted": True})
                        await wait_until(acknowledged)
                        self.assertEqual([name for name, _ in peer.calls],
                                         ["runner.hello", "runner.sync", "runner.ready", "jobs.started", "jobs.complete"])
                        self.assertIsNone((await repo.get("j1", "a1")).result_json["output"])
                    finally:
                        peer.closed.set()
                        await task

    async def test_cli_app_wiring_with_real_websocket_and_agent_http(self):
        """实际 run_client 默认创建 Manager；两端网络均使用本机临时端口。"""
        with tempfile.TemporaryDirectory() as directory:
            requests, order = [], []
            completed = asyncio.Event()
            agent_entered, release_agent = asyncio.Event(), asyncio.Event()

            async def agent(reader, writer):
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                    length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    requests.append(json.loads(await reader.readexactly(length)))
                    agent_entered.set()
                    await release_agent.wait()
                    body = json.dumps({"answer": "真实 HTTP 响应"}, ensure_ascii=False).encode("utf-8")
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            async def cloud(socket):
                async with JsonRpcPeer(WebSocketConnection(socket)) as peer:
                    async def hello(**params):
                        self.assertEqual(params["capabilities"], ["http_json", "http_sse"])
                        return {"session_id": "s", "status": "ready"}
                    async def sync(**params):
                        return {"jobs": []}
                    async def started(**params):
                        order.append("started")
                        return {"acknowledged": True}
                    async def complete(**params):
                        order.append("complete")
                        self.assertEqual(params["status"], "succeeded")
                        self.assertEqual(params["output"], {"answer": "真实 HTTP 响应"})
                        completed.set()
                        return {"acknowledged": True}
                    async def ready(session_id):
                        return {"session_id": session_id, "status": "ready"}
                    for method, handler in (("runner.hello", hello), ("runner.sync", sync), ("runner.ready", ready),
                                            ("jobs.started", started), ("jobs.complete", complete)):
                        peer.register(method, handler)
                    while True:
                        reply = await peer.call("jobs.dispatch", dispatch_params())
                        if reply.get("accepted"):
                            break
                        self.assertEqual(reply["reason"], "not_ready")
                        await asyncio.sleep(0.01)
                    try:
                        await asyncio.wait_for(agent_entered.wait(), 3)
                        self.assertEqual(await peer.call("jobs.dispatch", dispatch_params()), {"accepted": True})
                        self.assertEqual(await peer.call("jobs.dispatch", dispatch_params("j2")),
                                         {"accepted": False, "reason": "busy"})
                    finally:
                        release_agent.set()
                    try:
                        await peer.wait_closed()
                    except TransportClosed:
                        pass  # 测试主动退出客户端，属于预期的正常断开。

            async with await asyncio.start_server(agent, "127.0.0.1", 0) as agent_server:
                async with serve(cloud, "127.0.0.1", 0) as cloud_server:
                    url = f"ws://127.0.0.1:{cloud_server.sockets[0].getsockname()[1]}"
                    config = ClientConfig(url, "runner", "token", Path(directory) / "state.sqlite3",
                                          targets={"agent": {"url": f"http://127.0.0.1:{agent_server.sockets[0].getsockname()[1]}/invoke"}},
                                          allow_insecure_localhost=True)
                    task = asyncio.create_task(run_client(config))
                    try:
                        await asyncio.wait_for(completed.wait(), 5)
                        # 让 ACK 确实落盘；使用真实仓储读取，不仅检查网络事件。
                        async with ClientDatabase(config.state_path) as db:
                            repo = AttemptRepository(db)
                            async def acknowledged():
                                row = await repo.get("j1", "a1")
                                return row is not None and row.delivery_status == "acknowledged"
                            await wait_until(acknowledged)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
            self.assertEqual(order, ["started", "complete"])
            self.assertEqual(requests, [{"question": "你好"}])
