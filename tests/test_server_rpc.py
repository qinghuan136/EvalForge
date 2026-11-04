"""无网络 RPC 边界测试；使用真实 JSON-RPC 库及可控双向文本通道。"""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import Mock

from starlette.websockets import WebSocket

from evalforge.server.rpc import JsonRpcPeer, RpcError, RpcProtocolError
from evalforge.server.ws_transport import TransportClosed, WebSocketConnection


class Channel:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.other = None
        self.closed = False

    async def send_text(self, message):
        if self.closed:
            raise TransportClosed("Closed")
        await self.other.messages.put(message)

    async def receive_text(self):
        message = await self.messages.get()
        if message is None:
            raise TransportClosed("Closed")
        return message

    async def close(self):
        if not self.closed:
            self.closed = self.other.closed = True
            self.messages.put_nowait(None)
            self.other.messages.put_nowait(None)


def channels():
    left, right = Channel(), Channel()
    left.other, right.other = right, left
    return left, right


@asynccontextmanager
async def peers(**options):
    left, right = channels()
    async with JsonRpcPeer(left, **options) as a, JsonRpcPeer(right, **options) as b:
        yield a, b


class ServerRPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_authorization_waits_for_transaction_cleanup(self):
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def authorize():
            entered.set()
            try:
                await release.wait()
            finally:
                cleaned.set()
        connection = WebSocketConnection(Mock(spec=WebSocket), authorize)
        task = asyncio.create_task(connection.check_authorization())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())

    async def test_nested_reverse_calls_and_out_of_order_replies(self):
        async with peers() as (a, b):
            release = asyncio.Event()
            async def echo(value):
                if value == "slow":
                    await release.wait()
                else:
                    release.set()
                return value
            async def reverse(value):
                return await b.call("double", [value], timeout=1)
            async def double(value):
                return value * 2
            a.register("double", double)
            b.register("echo", echo)
            b.register("reverse", reverse)
            self.assertEqual(await asyncio.gather(a.call("echo", ["slow"]), a.call("echo", ["fast"])), ["slow", "fast"])
            self.assertEqual(await a.call("reverse", [21]), 42)

    async def test_timeout_and_cancellation_release_pending_capacity(self):
        async with peers(max_pending=1) as (a, b):
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow():
                entered.set()
                await release.wait()
                return "late"
            async def echo(value):
                return value
            b.register("slow", slow)
            b.register("echo", echo)
            task = asyncio.create_task(a.call("slow", timeout=10))
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(RuntimeError):
                await a.call("echo", [1])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(TimeoutError):
                await a.call("slow", timeout=0.02)
            release.set()
            self.assertEqual(await a.call("echo", ["still-open"]), "still-open")

    async def test_inbound_handler_capacity_is_bounded(self):
        async with peers(max_handlers=1) as (a, b):
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow():
                entered.set()
                await release.wait()
                return True
            b.register("slow", slow)
            task = asyncio.create_task(a.call("slow"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                with self.assertRaises(RpcError) as caught:
                    await a.call("slow")
                self.assertEqual(caught.exception.code, -32000)
            finally:
                release.set()
                await task

    async def test_malformed_response_does_not_poison_later_calls(self):
        left, right = channels()
        async with JsonRpcPeer(left) as peer:
            for changes in ({"jsonrpc": "1.0", "result": 1}, {"result": 1, "error": {}},
                            {"error": {"code": True, "message": "bad"}}):
                call = asyncio.create_task(peer.call("test", timeout=1))
                request = json.loads(await right.receive_text())
                await right.send_text(json.dumps({"jsonrpc": "2.0", "id": request["id"], **changes}))
                with self.assertRaises(RpcProtocolError):
                    await call
            call = asyncio.create_task(peer.call("test", timeout=1))
            request = json.loads(await right.receive_text())
            await right.send_text(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "ok"}))
            self.assertEqual(await call, "ok")

    async def test_disconnect_fails_waiters_and_cancels_handlers(self):
        async with peers() as (a, b):
            entered, cancelled = asyncio.Event(), asyncio.Event()
            async def blocked():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            b.register("blocked", blocked)
            task = asyncio.create_task(a.call("blocked"))
            await asyncio.wait_for(entered.wait(), 1)
            await b.close()
            with self.assertRaises(TransportClosed):
                await task
            await asyncio.wait_for(cancelled.wait(), 1)

    async def test_strict_json_and_batch_boundaries(self):
        left, right = channels()
        async with JsonRpcPeer(left, max_batch=2):
            for message, code in (("{bad", -32700), ('{"x":NaN}', -32700), ('{"x":1e999}', -32700),
                                  ("[]", -32600), ("[1,2,3]", -32600), ("[[{}]]", -32600)):
                await right.send_text(message)
                response = json.loads(await asyncio.wait_for(right.receive_text(), 1))
                if isinstance(response, list):
                    response = response[0]
                self.assertEqual(response["error"]["code"], code)

    async def test_handler_can_close_peer_without_deadlock(self):
        async with peers() as (a, b):
            async def stop():
                await b.close()
            b.register("stop", stop)
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(a.call("stop"), 1)
