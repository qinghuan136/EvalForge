import asyncio
import json
import gzip
import os
import unittest
from unittest.mock import patch

import httpx

from evalforge_client.config import ClientConfig
from evalforge_client.http_executor import HttpExecutor
from evalforge_client.targets import AgentTarget


TARGETS = {"agent": {"url": "http://agent.example/invoke"}}


class SlowBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        yield b'{"reply":'
        await asyncio.sleep(0.1)
        yield b'"done"}'

    async def aclose(self):
        self.closed = True


class HttpExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_preserves_json_values_including_top_level_null(self):
        requests = []

        async def agent(request):
            requests.append(request)
            return httpx.Response(200, json={"received": json.loads(request.content)})

        async with HttpExecutor(TARGETS, transport=httpx.MockTransport(agent)) as executor:
            for value in ({"中文": [True, None, {"nested": 1}]}, [1, "hi"], "text", 0, False, None):
                result = await executor.execute("agent", value, timeout_seconds=1)
                self.assertIsNone(result.error)
                self.assertEqual(result.output, {"received": value})
                self.assertEqual(result.http_status, 200)
                self.assertGreaterEqual(result.duration_ms, 0)
                self.assertEqual(requests[-1].method, "POST")
                self.assertEqual(requests[-1].headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(requests[-1].content), value)

    async def test_configured_headers_and_env_credentials_are_resolved_locally(self):
        async def agent(request):
            self.assertEqual(request.headers["Authorization"], "Bearer private-token")
            self.assertEqual(request.headers["X-Mode"], "evaluation")
            return httpx.Response(200, json={"success": False})

        with patch.dict(os.environ, {"TEST_AGENT_AUTH": "Bearer private-token"}):
            targets = {"agent": {"url": TARGETS["agent"]["url"], "headers": {"X-Mode": "evaluation"},
                                 "headers_env": {"Authorization": "TEST_AGENT_AUTH"}}}
            config = ClientConfig("wss://cloud.example/runner", "r1", "token", "unused.sqlite3", targets=targets)
            self.assertNotIn("private-token", repr(AgentTarget.from_config(targets["agent"])))
            async with HttpExecutor(config.targets, transport=httpx.MockTransport(agent)) as executor:
                result = await executor.execute("agent", {}, timeout_seconds=1)
            self.assertIsNone(result.error)  # 业务 success=false 不等于 HTTP 调用失败。
            self.assertEqual(result.output, {"success": False})

    async def test_non_2xx_keeps_json_body_and_redirect_is_not_followed(self):
        for status in (302, 400, 500):
            requests = []

            async def agent(request):
                requests.append(request)
                return httpx.Response(status, json={"details": "upstream error"},
                                      headers={"Location": "http://other.example"})

            async with HttpExecutor(TARGETS, transport=httpx.MockTransport(agent)) as executor:
                result = await executor.execute("agent", {}, timeout_seconds=1)
            self.assertEqual(result.error["code"], "http_error")
            self.assertEqual(result.output, {"details": "upstream error"})
            self.assertEqual(result.http_status, status)
            self.assertEqual(len(requests), 1)

    async def test_json_response_contract_and_non_2xx_error_precedence(self):
        for status, body, code in ((200, b"plain text", "invalid_json"), (204, b"", "invalid_json"),
                                   (200, b'{"score": NaN}', "invalid_json"),
                                   (200, b'{"score": 1e999}', "invalid_json"),
                                   (200, b'"\xff"', "invalid_json"), (500, b"plain error", "http_error")):
            async with HttpExecutor(TARGETS, transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, content=body))) as executor:
                result = await executor.execute("agent", {}, timeout_seconds=1)
            self.assertEqual(result.error["code"], code)
            self.assertIsNone(result.output)
            self.assertEqual(result.http_status, status)

    async def test_all_json_response_types_are_preserved(self):
        for value in (None, False, 0, "answer", [1, 2], {"answer": {"nested": []}}):
            async with HttpExecutor(TARGETS, transport=httpx.MockTransport(
                    lambda request: httpx.Response(201, json=value) if value is not None
                    else httpx.Response(201, content=b"null"))) as executor:
                result = await executor.execute("agent", {}, timeout_seconds=1)
                self.assertEqual(result.output, value)
                self.assertIsNone(result.error)

    async def test_network_failures_are_not_retried_or_leaked(self):
        for exception, expected in ((httpx.ConnectError, "connection_error"),
                                    (httpx.ReadTimeout, "timeout"), (httpx.ReadError, "request_error")):
            calls = []

            async def agent(request):
                calls.append(request)
                raise exception("private-token: secret-url-and-body", request=request)

            async with HttpExecutor(TARGETS, transport=httpx.MockTransport(agent)) as executor:
                result = await executor.execute("agent", {}, timeout_seconds=1)
            self.assertEqual(result.error["code"], expected)
            self.assertNotIn("private-token", str(result.error))
            self.assertIsNone(result.http_status)
            self.assertEqual(len(calls), 1)

    async def test_total_deadline_includes_response_body_and_closes_it(self):
        body = SlowBody()
        async with HttpExecutor(TARGETS, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body))) as executor:
            result = await executor.execute("agent", {}, timeout_seconds=0.03)
        self.assertEqual(result.error["code"], "timeout")
        self.assertEqual(result.http_status, 200)
        self.assertTrue(body.closed)

    async def test_cancellation_propagates_and_closes_response(self):
        body = SlowBody()
        async with HttpExecutor(TARGETS, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body))) as executor:
            task = asyncio.create_task(executor.execute("agent", {}, timeout_seconds=2))
            await asyncio.wait_for(body.entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(body.closed)

    async def test_large_body_is_rejected_and_response_is_closed(self):
        async with HttpExecutor(TARGETS, max_response_bytes=4, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b'{"too":"large"}'))) as executor:
            result = await executor.execute("agent", {}, timeout_seconds=1)
        self.assertEqual(result.error["code"], "response_too_large")
        self.assertEqual(result.http_status, 200)

    async def test_compressed_response_is_rejected_before_decompression(self):
        compressed = gzip.compress(b"x" * (2 * 1024 * 1024))
        async def agent(request):
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(compressed))

        async with HttpExecutor(TARGETS, max_response_bytes=1024, transport=httpx.MockTransport(agent)) as executor:
            result = await executor.execute("agent", {}, timeout_seconds=1)
        self.assertEqual(result.error["code"], "unsupported_encoding")

    async def test_response_cookies_are_not_shared_across_calls_or_targets(self):
        requests = []
        async def agent(request):
            requests.append(request)
            self.assertNotIn("Cookie", request.headers)
            return httpx.Response(200, json={}, headers={"Set-Cookie": "session=private; Path=/"})

        targets = {"one": TARGETS["agent"], "two": {"url": "http://agent.example/other"}}
        async with HttpExecutor(targets, transport=httpx.MockTransport(agent)) as executor:
            await executor.execute("one", {}, timeout_seconds=1)
            await executor.execute("two", {}, timeout_seconds=1)
            await asyncio.gather(executor.execute("one", {}, timeout_seconds=1),
                                 executor.execute("two", {}, timeout_seconds=1))
        self.assertEqual(len(requests), 4)

    async def test_invalid_invocations_fail_before_network_and_lifecycle_is_explicit(self):
        calls = []

        async def agent(request):
            calls.append(request)
            return httpx.Response(200, json={})

        executor = HttpExecutor(TARGETS, transport=httpx.MockTransport(agent))
        with self.assertRaises(RuntimeError):
            await executor.execute("agent", {}, timeout_seconds=1)
        async with executor:
            with self.assertRaises(RuntimeError):
                await executor.__aenter__()
            for target, value, timeout in (("missing", {}, 1), ("agent", {}, 0), ("agent", {}, True),
                                           ("agent", {}, float("inf")), ("agent", {1: "invalid"}, 1),
                                           ("agent", float("nan"), 1)):
                with self.assertRaises(ValueError):
                    await executor.execute(target, value, timeout_seconds=timeout)
        with self.assertRaises(RuntimeError):
            await executor.execute("agent", {}, timeout_seconds=1)
        self.assertEqual(calls, [])

    def test_target_configuration_rejects_bad_urls_headers_and_missing_credentials(self):
        bad = [
            {"url": "file:///private"}, {"url": "http://user:pass@agent.example"},
            {"url": "http://agent.example", "method": "GET"},
            {"url": "http://agent.example", "headers": {"Content-Length": "1"}},
            {"url": "http://agent.example", "headers": {"Bad Name": "x"}},
            {"url": "http://agent.example", "headers": {"Authorization": "secret\r\nx"}},
            {"url": "http://agent.example", "headers": {"X-Key": "one"}, "headers_env": {"x-key": "ENV"}},
            {"url": "http://agent.example", "headers_env": {"Authorization": "UNSET_TEST_AGENT_KEY"}},
        ]
        with patch.dict(os.environ, {}, clear=True):
            for target in bad:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    AgentTarget.from_config(target)

    async def test_real_loopback_http_request_and_response(self):
        captured = []
        finished = asyncio.Event()

        async def agent(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                length = next(int(line.split(":", 1)[1]) for line in head.split("\r\n")
                              if line.lower().startswith("content-length:"))
                value = json.loads(await reader.readexactly(length))
                captured.append((head.split("\r\n")[0], value))
                body = json.dumps({"answer": value}, ensure_ascii=False).encode("utf-8")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        async with await asyncio.start_server(agent, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with HttpExecutor({"local": {"url": f"http://127.0.0.1:{port}/invoke"}}) as executor:
                result = await executor.execute("local", {"question": "你好"}, timeout_seconds=2)
            await asyncio.wait_for(finished.wait(), 2)
        self.assertIsNone(result.error)
        self.assertEqual(result.output, {"answer": {"question": "你好"}})
        self.assertEqual(captured, [("POST /invoke HTTP/1.1", {"question": "你好"})])


if __name__ == "__main__":
    unittest.main()
