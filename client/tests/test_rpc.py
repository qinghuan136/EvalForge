import asyncio
import json
import unittest

from websockets.asyncio.client import connect

from evalforge_client.rpc import JsonRpcPeer, RpcError, RpcProtocolError
from evalforge_client.transport import TransportClosed
from helpers import rpc_pair, rpc_server


async def echo(value):
    return value


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_bidirectional_calls_and_response_matching_out_of_order(self):
        release_slow = asyncio.Event()
        completed = []

        async def ordered(value):
            if value == "slow":
                await release_slow.wait()
            else:
                release_slow.set()
            completed.append(value)
            return value

        async with rpc_pair({"echo": ordered}) as (client, server):
            client.register("client.echo", echo)
            values = await asyncio.gather(client.call("echo", ["slow"]), client.call("echo", ["fast"]))
            self.assertEqual(values, ["slow", "fast"])
            self.assertEqual(completed, ["fast", "slow"])
            self.assertEqual(await server.call("client.echo", {"value": {"中文": None}}), {"中文": None})

    async def test_handler_can_call_back_without_blocking_receiver(self):
        server_peer = None

        async def callback(value):
            return await server_peer.call("double", [value], timeout=2)

        async def double(value):
            return value * 2

        async with rpc_pair({"callback": callback}) as (client, server):
            server_peer = server
            client.register("double", double)
            self.assertEqual(await client.call("callback", [21], timeout=3), 42)

    async def test_remote_errors_and_invalid_arguments(self):
        async def custom():
            raise RpcError(1001, "Rejected", {"reason": "test"})

        async def broken():
            raise RuntimeError("secret must not cross the wire")

        async def invalid_output():
            return float("nan")

        async with rpc_pair({"echo": echo, "custom": custom, "broken": broken,
                             "invalid_output": invalid_output}) as (client, _):
            for method, params, code in (
                ("missing", {}, -32601), ("echo", {"wrong": 1}, -32602),
                ("broken", {}, -32603), ("invalid_output", {}, -32603),
            ):
                with self.subTest(method=method), self.assertRaises(RpcError) as caught:
                    await client.call(method, params)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))
            with self.assertRaises(RpcError) as caught:
                await client.call("custom")
            self.assertEqual(caught.exception.data, {"reason": "test"})

    async def test_notification_executes_without_any_response_even_on_error(self):
        notified = asyncio.Event()

        async def notification(value):
            self.assertEqual(value, 7)
            notified.set()
            raise RuntimeError("notification failed")

        async with rpc_server({"notification": notification}) as (url, _):
            async with connect(url, proxy=None) as socket:
                await socket.send(json.dumps({"jsonrpc": "2.0", "method": "notification", "params": [7]}))
                await asyncio.wait_for(notified.wait(), 2)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(socket.recv(), 0.05)

    async def test_parse_invalid_request_null_id_and_batch(self):
        async with rpc_server({"echo": echo}) as (url, _):
            async with connect(url, proxy=None) as socket:
                for payload, code in (
                    ("{bad", -32700), ('{"x": NaN}', -32700),
                    ('{"x": 1e999}', -32700), ("[]", -32600),
                    ("7", -32600),
                    ('{"jsonrpc":"2.0","method":"echo","id":true}', -32600),
                    # 库按请求 schema 将 params:null 判为无效请求；缺失函数参数仍是 -32602。
                    ('{"jsonrpc":"2.0","method":"echo","params":null,"id":1}', -32600),
                ):
                    await socket.send(payload)
                    reply = json.loads(await asyncio.wait_for(socket.recv(), 2))
                    self.assertEqual(reply["error"]["code"], code)
                await socket.send(json.dumps([
                    {"jsonrpc": "2.0", "method": "echo", "params": ["one"], "id": 1},
                    {"jsonrpc": "2.0", "method": "echo", "params": ["no reply"]},
                    {"jsonrpc": "2.0", "method": "echo", "params": ["null"], "id": None},
                    {"jsonrpc": "2.0", "method": "unknown", "id": "last"},
                ]))
                replies = json.loads(await asyncio.wait_for(socket.recv(), 2))
                self.assertEqual([reply["id"] for reply in replies], [1, None, "last"])
                self.assertEqual(replies[0]["result"], "one")
                self.assertEqual(replies[-1]["error"]["code"], -32601)

    async def test_timeout_and_cancellation_do_not_replay_or_cancel_remote_work(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        calls = []

        async def delayed(value):
            calls.append(value)
            entered.set()
            await release.wait()
            return value

        async with rpc_pair({"delayed": delayed, "echo": echo}) as (client, _):
            with self.assertRaises(TimeoutError):
                await client.call("delayed", [1], timeout=0.05)
            task = asyncio.create_task(client.call("delayed", [2]))
            entered.clear()
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            self.assertEqual(await client.call("echo", [3]), 3)
            self.assertEqual(calls, [1, 2])

    async def test_disconnect_fails_pending_call_and_stops_handlers(self):
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async with rpc_pair({"block": block}) as (client, server):
            waiting = asyncio.create_task(client.call("block", timeout=5))
            await asyncio.wait_for(entered.wait(), 2)
            await server.close()
            with self.assertRaises(TransportClosed):
                await asyncio.wait_for(waiting, 2)
            await asyncio.wait_for(stopped.wait(), 2)

    async def test_slow_handler_does_not_block_capacity_rejection_or_responses(self):
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        async with rpc_pair({"block": block}, max_handlers=1) as (client, server):
            client.register("echo", echo)
            waiting = asyncio.create_task(client.call("block"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaises(RpcError) as caught:
                    await client.call("block")
                self.assertEqual(caught.exception.code, -32000)
                self.assertEqual(await server.call("echo", [8]), 8)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def test_handler_initiated_close_does_not_wait_on_itself(self):
        server_peer = None
        stopped = asyncio.Event()

        async def shutdown():
            try:
                await server_peer.close()
            finally:
                stopped.set()

        async with rpc_pair({"shutdown": shutdown}) as (client, server):
            server_peer = server
            with self.assertRaises(TransportClosed):
                await client.call("shutdown", timeout=2)
            await asyncio.wait_for(stopped.wait(), 2)

    async def test_invalid_response_fails_only_matching_call(self):
        # 用原始 WebSocket 对端发送坏响应及重复响应。
        async with rpc_server() as (url, ready):
            async with connect(url, proxy=None) as socket:
                server = await asyncio.wait_for(ready, 2)
                task = asyncio.create_task(server.call("remote", timeout=2))
                request = json.loads(await asyncio.wait_for(socket.recv(), 2))
                bad_reply = {"jsonrpc": "2.0", "id": request["id"], "result": 1, "error": {}}
                await socket.send(json.dumps(bad_reply))
                with self.assertRaises(RpcProtocolError):
                    await task
                await socket.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": 9}))
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(socket.recv(), 0.05)

    async def test_registration_and_outgoing_validation(self):
        async with rpc_pair() as (client, _):
            with self.assertRaises(ValueError):
                client.register("sync", lambda: None)
            client.register("echo", echo)
            with self.assertRaises(ValueError):
                client.register("echo", echo)
            for method, params in (("rpc.internal", {}), ("echo", None), ("echo", [float("nan")])):
                with self.subTest(method=method), self.assertRaises(ValueError):
                    await client.call(method, params)
            with self.assertRaises(ValueError):
                await client.call("echo", [], timeout=0)

    async def test_explicit_negative_rpc_error_keeps_application_data(self):
        async def reject():
            raise RpcError(-32001, "Rejected", {"reason": "unsupported"})

        async with rpc_pair({"reject": reject}) as (client, _):
            with self.assertRaises(RpcError) as caught:
                await client.call("reject")
            self.assertEqual(caught.exception.code, -32001)
            self.assertEqual(caught.exception.data, {"reason": "unsupported"})

    async def test_nested_batch_is_rejected_and_method_registry_is_per_peer(self):
        async with rpc_server({"echo": echo}) as (url, _):
            async with connect(url, proxy=None) as socket:
                await socket.send(json.dumps([[{"jsonrpc": "2.0", "method": "echo", "params": [1], "id": 1}]]))
                reply = json.loads(await asyncio.wait_for(socket.recv(), 2))
                self.assertEqual(reply[0]["error"]["code"], -32600)
        async with rpc_pair({}) as (client, _):
            with self.assertRaises(RpcError) as caught:
                await client.call("echo", [1])
            self.assertEqual(caught.exception.code, -32601)

    async def test_null_result_and_malformed_responses_with_client_library(self):
        async with rpc_pair({"echo": echo}) as (client, _):
            self.assertIsNone(await client.call("echo", [None]))
        async with rpc_server() as (url, ready):
            async with connect(url, proxy=None) as socket:
                peer = await asyncio.wait_for(ready, 2)
                for fields in (
                    {"jsonrpc": "1.0", "result": 1},
                    {"error": {"code": True, "message": "invalid"}},
                    {"error": []}, {},
                ):
                    pending = asyncio.create_task(peer.call("remote", timeout=2))
                    request = json.loads(await asyncio.wait_for(socket.recv(), 2))
                    await socket.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], **fields}))
                    with self.assertRaises(RpcProtocolError):
                        await pending


if __name__ == "__main__":
    unittest.main()
