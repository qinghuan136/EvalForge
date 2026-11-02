import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.job_manager import JobManager
from evalforge_client.rpc import RpcError
from evalforge_client.session import RunnerSession

from test_job_lifecycle import dispatch_params, wait_until
from test_session import FakePeer


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self):
        self.entered, self.release, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        await self.release.wait()
        yield b'{"answer":"ok"}'

    async def aclose(self):
        self.closed.set()


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.path = Path(self.temp.name) / "runner.sqlite3"
        self.db = await self.stack.enter_async_context(ClientDatabase(self.path))
        self.repo = AttemptRepository(self.db)
        self.body, self.requests = WaitingBody(), []

        async def agent(request):
            self.requests.append(request)
            return httpx.Response(200, stream=self.body)

        self.executor = await self.stack.enter_async_context(HttpExecutor(
            {"agent": {"url": "http://agent.example/invoke"}}, transport=httpx.MockTransport(agent),
        ))
        self.jobs = await self.stack.enter_async_context(JobManager(self.repo, self.executor))

    async def seed(self):
        return await self.repo.accept(job_id="j1", attempt_id="a1", agent_target_id="agent", example_id="e1",
                                      input_json={"question": "你好"}, timeout_seconds=30)

    async def start(self):
        await self.jobs.dispatch(dispatch_params(timeout_seconds=30))
        await asyncio.wait_for(self.body.entered.wait(), 3)
        return await self.repo.get("j1", "a1")

    async def final(self):
        async def pending():
            return (await self.repo.get("j1", "a1")).delivery_status == "pending"
        await wait_until(pending)
        return await self.repo.get("j1", "a1")

    async def test_cancel_before_execution_never_calls_agent_and_is_idempotent(self):
        row = await self.seed()
        await self.jobs.cancel(row, "user_requested")
        first = await self.final()
        await self.jobs.cancel(row, "duplicate")
        await self.jobs.resume(first)
        self.assertEqual(first.execution_status, "cancelled")
        self.assertIsNone(first.started_at)
        self.assertEqual(first.cancel_reason, "user_requested")
        self.assertEqual((await self.repo.get("j1", "a1")).result_json, first.result_json)
        self.assertEqual(self.requests, [])

    async def test_cancel_running_closes_http_only_and_persists_result(self):
        row = await self.start()
        await asyncio.gather(self.jobs.cancel(row, "first"), self.jobs.cancel(row, "duplicate"))
        result = await self.final()
        self.assertEqual(result.execution_status, "cancelled")
        self.assertEqual(result.cancel_reason, "first")
        self.assertEqual(result.result_json["error"]["code"], "cancelled")
        self.assertIn("remote Agent may still be running", result.result_json["error"]["message"])
        self.assertGreater(result.result_json["duration_ms"], 0)
        self.assertTrue(self.body.closed.is_set())
        self.assertEqual([(r.method, r.url.path) for r in self.requests], [("POST", "/invoke")])
        # pending 的取消结果不占新执行容量；没有任何 Agent 取消接口请求。
        self.body.release.set()
        self.assertEqual(await self.jobs.dispatch(dispatch_params("j2")), {"accepted": True})
        async def next_done():
            row = await self.repo.get("j2", "a1")
            return row.delivery_status == "pending"
        await wait_until(next_done)
        self.assertEqual(len(self.requests), 2)

    async def test_cancel_after_result_is_immutable_even_after_ack(self):
        self.body.release.set()
        row = await self.start()
        first = await self.final()
        await self.repo.acknowledge("j1", "a1")
        await self.jobs.cancel(row, "too_late")
        final = await self.repo.get("j1", "a1")
        self.assertEqual(final.execution_status, "succeeded")
        self.assertEqual(final.delivery_status, "acknowledged")
        self.assertEqual(final.result_json, first.result_json)
        self.assertEqual(final.cancel_requested, 0)

    async def test_repeated_cancel_and_resume_do_not_interrupt_http_cleanup(self):
        cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()

        class DelayedCloseBody(WaitingBody):
            async def aclose(body):
                cleanup_entered.set()
                await release_cleanup.wait()
                body.closed.set()

        self.body = DelayedCloseBody()
        row = await self.start()
        await self.jobs.cancel(row, "first")
        try:
            await asyncio.wait_for(cleanup_entered.wait(), 3)
            await self.jobs.cancel(row, "duplicate")
            await self.jobs.resume(await self.repo.get("j1", "a1"))
        finally:
            release_cleanup.set()
        result = await self.final()
        self.assertTrue(self.body.closed.is_set())
        self.assertEqual(result.execution_status, "cancelled")
        self.assertEqual(result.cancel_reason, "first")

    async def test_real_http_cancel_closes_socket_without_stopping_remote_work(self):
        entered, disconnected = asyncio.Event(), asyncio.Event()
        release_agent, agent_finished = asyncio.Event(), asyncio.Event()
        handlers, request_lines = set(), []

        async def agent(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                request_lines.append(header.split(b"\r\n", 1)[0])
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                await reader.readexactly(length)
                entered.set()
                await reader.read()  # 客户端取消后关闭连接，不会再发一个取消接口请求。
                disconnected.set()
                await release_agent.wait()
                agent_finished.set()  # 远端工作仍可独立继续。
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        try:
            async with await asyncio.start_server(agent, "127.0.0.1", 0) as server:
                endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/invoke"
                async with HttpExecutor({"agent": {"url": endpoint}}) as executor:
                    async with JobManager(self.repo, executor) as jobs:
                        await jobs.dispatch(dispatch_params(timeout_seconds=30))
                        await asyncio.wait_for(entered.wait(), 3)
                        await jobs.cancel(await self.repo.get("j1", "a1"), "user_requested")
                        result = await self.final()
                        await asyncio.wait_for(disconnected.wait(), 3)
                        self.assertEqual(result.execution_status, "cancelled")
                        self.assertFalse(agent_finished.is_set())
                        release_agent.set()
                        await asyncio.wait_for(agent_finished.wait(), 3)
            self.assertEqual(request_lines, [b"POST /invoke HTTP/1.1"])
        finally:
            release_agent.set()
            for handler in list(handlers):
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    async def test_cancel_during_result_save_preserves_obtained_response(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.repo.finish

        async def finish(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.body.release.set()
        with patch.object(self.repo, "finish", side_effect=finish):
            await self.start()
            await asyncio.wait_for(entered.wait(), 3)
            try:
                await self.jobs.cancel(await self.repo.get("j1", "a1"), "late")
                self.assertEqual((await self.repo.get("j1", "a1")).cancel_requested, 1)
            finally:
                release.set()
            row = await self.final()
        self.assertEqual(row.execution_status, "succeeded")
        self.assertEqual(row.result_json["output"], {"answer": "ok"})
        self.assertEqual(len(self.requests), 1)

    async def test_disconnect_of_cancel_caller_cannot_interrupt_local_cancellation(self):
        row = await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.repo.request_cancel

        async def request_cancel(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        with patch.object(self.repo, "request_cancel", side_effect=request_cancel):
            caller = asyncio.create_task(self.jobs.cancel(row, "user_requested"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
            finally:
                release.set()
            result = await self.final()
        self.assertEqual(result.execution_status, "cancelled")
        self.assertTrue(self.body.closed.is_set())

    async def test_unknown_retains_cancel_intent_without_claiming_stopped(self):
        row = await self.seed()
        await self.repo.claim("j1", "a1")
        await self.repo.recover_interrupted()
        await self.jobs.cancel(row, "first")
        await self.jobs.cancel(row, "second")
        unknown = await self.repo.get("j1", "a1")
        self.assertEqual(unknown.execution_status, "unknown")
        self.assertEqual(unknown.cancel_reason, "first")
        self.assertEqual(unknown.delivery_status, "not_ready")
        self.assertIsNone(unknown.result_json)
        self.assertEqual(self.requests, [])

    async def test_session_cancel_is_confirmed_then_uploaded_and_acknowledged(self):
        config = ClientConfig("wss://cloud.example/runner", "runner", "token", self.path)
        peer = FakePeer({"runner.hello": {"session_id": "s1", "status": "ready"},
                         "runner.sync": {"jobs": []}, "jobs.started": {"acknowledged": True},
                         "jobs.complete": {"acknowledged": True}})
        session = RunnerSession(config, self.repo, self.jobs)
        task = asyncio.create_task(session.run(peer))
        async def ready():
            return session.ready
        async def acknowledged():
            return (await self.repo.get("j1", "a1")).delivery_status == "acknowledged"
        try:
            await wait_until(ready)
            await peer.handlers["jobs.dispatch"](**dispatch_params(timeout_seconds=30))
            await asyncio.wait_for(self.body.entered.wait(), 3)
            self.assertEqual(await peer.handlers["jobs.cancel"]("missing", "a1"),
                             {"accepted": False, "reason": "attempt_not_found"})
            with self.assertRaises(RpcError) as invalid:
                await peer.handlers["jobs.cancel"]("j1", "a1", reason={})
            self.assertEqual(invalid.exception.code, -32602)
            self.assertEqual(await peer.handlers["jobs.cancel"]("j1", "a1", "user_requested"), {"accepted": True})
            await wait_until(acknowledged)
            result = (await self.repo.get("j1", "a1")).result_json
            self.assertEqual(result["status"], "cancelled")
            self.assertTrue(self.body.closed.is_set())
        finally:
            peer.closed.set()
            await task

    async def test_shutdown_during_result_save_waits_for_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.repo.finish

        async def finish(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.body.release.set()
        with patch.object(self.repo, "finish", side_effect=finish):
            await self.start()
            await asyncio.wait_for(entered.wait(), 3)
            closing = asyncio.create_task(self.jobs.__aexit__(None, None, None))
            try:
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
            finally:
                release.set()
                await closing
        self.assertEqual((await self.final()).execution_status, "succeeded")
