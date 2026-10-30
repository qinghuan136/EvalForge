"""临时本地 Cloud 与客户端互相调用；不包含会话或 Job 协议。"""

import asyncio
import json

from websockets.asyncio.server import serve

from evalforge_client.rpc import JsonRpcPeer
from evalforge_client.transport import TransportClosed, WebSocketConnection, WebSocketTransport


async def cloud(socket):
    async with JsonRpcPeer(WebSocketConnection(socket)) as peer:
        async def roundtrip(value):
            client_reply = await peer.call("demo.client_echo", {"value": value})
            return {"cloud_received": value, "client_reply": client_reply}

        peer.register("demo.roundtrip", roundtrip)
        try:
            await peer.wait_closed()
        except TransportClosed:
            pass


async def main():
    async with serve(cloud, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = WebSocketTransport(
            f"ws://127.0.0.1:{port}", allow_insecure_localhost=True,
        )
        async with transport.open() as connection:
            async with JsonRpcPeer(connection) as peer:
                async def client_echo(value):
                    return value

                peer.register("demo.client_echo", client_echo)
                result = await peer.call("demo.roundtrip", {"value": {"message": "你好，EvalForge"}})
                print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
