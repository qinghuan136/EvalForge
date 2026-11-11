import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from evalforge_client.attempts import AttemptRepository
from evalforge_client.config import ClientConfig
from evalforge_client.database import ClientDatabase
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.job_manager import JobManager
from evalforge_client.session import RunnerSession
from evalforge_client.stream_output import OutputChunk
from evalforge_client.transport import TransportClosed

from helpers import rpc_pair
from test_job_lifecycle import dispatch_params, wait_until


TARGETS = {"agent": {"url": "http://agent.example/invoke"}}
HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}


class Body(httpx.AsyncByteStream):
    def __init__(self, *parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, asyncio.Event):
                await part.wait()
            else:
                yield part

    async def aclose(self):
        self.closed = True


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_framing_survives_fragmentation_and_result_ends_the_http_wait(self):
        raw = ("\ufeff: heartbeat\r\n\r\nevent: delta\r\ndata: 你\r\ndata: 好\r\n\r\n"
               "event: delta\rdata: 世界\r\r"
               'event: result\ndata: {"reply":"完整",\ndata: "sources":[1]}\n\n').encode("utf-8")
        body = Body(*(bytes([byte]) for byte in raw), asyncio.Event())
        chunks = []

        def agent(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), TARGETS["agent"]["url"])
            self.assertEqual(request.headers["Accept"], "text/event-stream")
            self.assertEqual(json.loads(request.content), {"query": "你好"})
            return httpx.Response(200, headers=HEADERS, stream=body)

        async with HttpExecutor(TARGETS, transport=httpx.MockTransport(agent)) as executor:
            result = await executor.execute("agent", {"query": "你好"}, timeout_seconds=2,
                                            stream_output=True, on_chunk=chunks.append)
        self.assertEqual(chunks, [OutputChunk(1, "你\n好"), OutputChunk(2, "世界")])
        self.assertIsNone(result.error)
        self.assertEqual(result.output, {"reply": "完整", "sources": [1]})
        self.assertTrue(body.closed)

    async def test_bad_streams_fail_instead_of_grading_partial_output(self):
        cases = (
            (200, {"Content-Type": "application/json"}, b'{"reply":1}', "unexpected_content_type"),
            (200, HEADERS, b"event: delta\ndata: partial\n\n", "incomplete_stream"),
            (200, HEADERS, b"event: result\ndata: null\n", "incomplete_stream"),
            (200, HEADERS, b"event: result\ndata: NaN\n\n", "invalid_json"),
            (200, HEADERS, b"event: result\ndata: \xff\n\n", "invalid_sse"),
            (200, HEADERS, b": " + b"x" * 100, "response_too_large"),
            (503, {"Content-Type": "application/json"}, b'{"reason":"offline"}', "http_error"),
        )
        for status, headers, content, code in cases:
            with self.subTest(code=code):
                async with HttpExecutor(TARGETS, max_response_bytes=100, transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, headers=headers, content=content),
                )) as executor:
                    result = await executor.execute("agent", None, timeout_seconds=1, stream_output=True)
                self.assertEqual(result.http_status, status)
                self.assertEqual(result.error["code"], code)
                self.assertEqual(result.output, {"reason": "offline"} if code == "http_error" else None)

    async def test_timeout_and_cancellation_close_an_open_stream(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                body = Body(b"event: delta\ndata: partial\n\n", asyncio.Event())
                received = asyncio.Event()
                async with HttpExecutor(TARGETS, transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, headers=HEADERS, stream=body),
                )) as executor:
                    task = asyncio.create_task(executor.execute(
                        "agent", None, timeout_seconds=3 if cancel else 0.1, stream_output=True,
                        on_chunk=lambda chunk: received.set(),
                    ))
                    await asyncio.wait_for(received.wait(), 2)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        self.assertEqual((await task).error["code"], "timeout")
                self.assertTrue(body.closed)

    async def test_live_preview_then_disconnect_preserves_final_result_for_replay(self):
        release_result = asyncio.Event()
        body = Body(b"event: delta\ndata: first\n\n", release_result,
                    b'event: delta\ndata: offline\n\nevent: result\ndata: {"reply":"final"}\n\n')
        previews, results, requests, snapshots = [], [], [], []
        received = asyncio.Event()

        def agent(request):
            requests.append(request)
            return httpx.Response(200, headers=HEADERS, stream=body)

        async def hello(**params):
            self.assertEqual(params["capabilities"], ["http_json", "http_sse"])
            return {"session_id": "s", "status": "ready"}

        async def sync(**params):
            snapshots.append(params["pending_results"])
            return {"jobs": [{"job_id": row["job_id"], "attempt_id": row["attempt_id"], "action": "retry_upload"}
                             for row in params["pending_results"]]}

        async def ready(session_id):
            return {"session_id": session_id, "status": "ready"}

        async def started(**params):
            await asyncio.Event().wait()  # 预览不用等待 started ACK。

        async def preview(**params):
            previews.append(params)
            received.set()

        async def complete(**params):
            results.append(params)
            return {"acknowledged": True}

        methods = {"runner.hello": hello, "runner.sync": sync, "runner.ready": ready,
                   "jobs.started": started, "jobs.output_chunk": preview, "jobs.complete": complete}

        with tempfile.TemporaryDirectory() as directory:
            config = ClientConfig("wss://cloud.example/runner", "runner", "token", Path(directory) / "state.sqlite3")
            async with ClientDatabase(config.state_path) as db, HttpExecutor(
                TARGETS, transport=httpx.MockTransport(agent),
            ) as executor:
                repo = AttemptRepository(db)
                async with JobManager(repo, executor) as jobs:
                    async with rpc_pair(methods) as (peer, cloud):
                        session = RunnerSession(config, repo, jobs)
                        task = asyncio.create_task(session.run(peer))
                        try:
                            async def is_ready():
                                return session.ready
                            await wait_until(is_ready)
                            params = dispatch_params(stream_output=True, timeout_seconds=5)
                            self.assertEqual(await cloud.call("jobs.dispatch", params), {"accepted": True})
                            await asyncio.wait_for(received.wait(), 3)
                            self.assertEqual(previews, [{"job_id": "j1", "attempt_id": "a1",
                                                         "chunks": [{"seq": 1, "text": "first"}]}])
                            self.assertEqual(results, [])
                            self.assertIs((await repo.get("j1", "a1")).stream_output, True)
                            await cloud.close()
                            with self.assertRaises(TransportClosed):
                                await asyncio.wait_for(task, 3)
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                    release_result.set()

                    async def is_pending():
                        return (await repo.get("j1", "a1")).delivery_status == "pending"

                    await wait_until(is_pending)
                    saved = (await repo.get("j1", "a1")).result_json
                    self.assertEqual(saved["status"], "succeeded")
                    self.assertEqual(saved["output"], {"reply": "final"})

                    async with rpc_pair(methods) as (peer, _):
                        task = asyncio.create_task(RunnerSession(config, repo, jobs).run(peer))
                        try:
                            async def is_acked():
                                return (await repo.get("j1", "a1")).delivery_status == "acknowledged"
                            await wait_until(is_acked)
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    self.assertEqual(snapshots, [[], [saved]])
                    self.assertEqual(results, [saved])
                    self.assertEqual(len(previews), 1)
                    self.assertEqual(len(requests), 1)
