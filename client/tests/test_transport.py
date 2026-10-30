import asyncio
import unittest
from contextlib import aclosing
from unittest.mock import AsyncMock, patch

from websockets.asyncio.server import serve

from evalforge_client.transport import ConnectionFailed, TransportClosed, WebSocketTransport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_wss_required_and_credentials_not_in_repr(self):
        for url in ("ws://example.com", "http://localhost", "wss://user:password@example.com",
                    "wss://example.com/#fragment", "wss://localhost:0"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WebSocketTransport(url)
        with self.assertRaises(ValueError):
            WebSocketTransport("ws://example.com", allow_insecure_localhost=True)
        options = WebSocketTransport("wss://example.com/?private=value", bearer_token="secret")
        self.assertNotIn("secret", repr(options))
        self.assertNotIn("private", repr(options))
        for kwargs in ({"ping_interval": 0}, {"max_size": True}, {"reconnect_max": 0.5},
                       {"bearer_token": "bad\r\nheader"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WebSocketTransport("wss://example.com", **kwargs)

    async def test_text_roundtrip_handshake_auth_and_close(self):
        closed = asyncio.Event()
        received_token = []

        async def echo(socket):
            received_token.append(socket.request.headers.get("Authorization"))
            try:
                async for message in socket:
                    await socket.send(message)
            finally:
                closed.set()

        async with serve(echo, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}", bearer_token="test-token",
                                           allow_insecure_localhost=True)
            async with transport.open() as connection:
                await connection.send_text('{"中文":"测试"}')
                self.assertEqual(await connection.receive_text(), '{"中文":"测试"}')
            await asyncio.wait_for(closed.wait(), 2)
            self.assertEqual(received_token, ["Bearer test-token"])
            with self.assertRaises(TransportClosed):
                await connection.send_text("closed")

    async def test_reconnect_yields_new_socket_without_replaying_messages(self):
        connections, payloads = [], []

        async def handle(socket):
            connections.append(socket)
            if len(connections) == 1:
                payloads.append(await socket.recv())
                await socket.close()
            else:
                await socket.send("new connection")
                await socket.wait_closed()

        async with serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}", allow_insecure_localhost=True,
                                           reconnect_min=0.01, reconnect_max=0.02)
            async with aclosing(transport.connections()) as stream:
                first = await asyncio.wait_for(anext(stream), 2)
                await first.send_text("once")
                with self.assertRaises(TransportClosed):
                    await first.receive_text()
                second = await asyncio.wait_for(anext(stream), 2)
                self.assertIsNot(first, second)
                self.assertEqual(await second.receive_text(), "new connection")
            self.assertEqual(payloads, ["once"])

    async def test_fatal_auth_rejection_is_not_retried(self):
        def reject(connection, request):
            return connection.respond(401, "Unauthorized")

        async with serve(lambda socket: socket.wait_closed(), "127.0.0.1", 0,
                         process_request=reject) as server:
            port = server.sockets[0].getsockname()[1]
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}", allow_insecure_localhost=True)
            async with aclosing(transport.connections()) as stream:
                with self.assertRaises(ConnectionFailed) as caught:
                    await asyncio.wait_for(anext(stream), 2)
                self.assertFalse(caught.exception.retryable)

    async def test_transient_failures_back_off_and_cancel_cleanly(self):
        transport = WebSocketTransport("wss://example.com", reconnect_min=1, reconnect_max=2)
        fake_connection = AsyncMock()
        attempts = AsyncMock(side_effect=[ConnectionFailed(retryable=True),
                                          ConnectionFailed(retryable=True), fake_connection])
        with patch.object(WebSocketTransport, "_connect", attempts), \
                patch("evalforge_client.transport.random.uniform", side_effect=lambda low, high: high), \
                patch("evalforge_client.transport.asyncio.sleep", new_callable=AsyncMock) as sleep:
            async with aclosing(transport.connections()) as stream:
                self.assertIs(await anext(stream), fake_connection)
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
            fake_connection.close.assert_awaited_once()

    async def test_binary_and_oversized_messages_close_connection(self):
        for payload in (b"binary", "x" * 100):
            async def send(socket):
                await socket.send(payload)
                await socket.wait_closed()

            async with serve(send, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                transport = WebSocketTransport(f"ws://127.0.0.1:{port}", allow_insecure_localhost=True,
                                               max_size=32)
                async with transport.open() as connection:
                    with self.subTest(payload=payload), self.assertRaises(TransportClosed):
                        await connection.receive_text()


if __name__ == "__main__":
    unittest.main()
