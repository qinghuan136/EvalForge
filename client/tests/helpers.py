import asyncio
from contextlib import asynccontextmanager

from websockets.asyncio.server import serve

from evalforge_client.rpc import JsonRpcPeer
from evalforge_client.transport import TransportClosed, WebSocketConnection, WebSocketTransport


@asynccontextmanager
async def rpc_server(methods=None, **peer_options):
    ready = asyncio.get_running_loop().create_future()

    async def handle(socket):
        async with JsonRpcPeer(WebSocketConnection(socket), **peer_options) as peer:
            for name, handler in (methods or {}).items():
                peer.register(name, handler)
            ready.set_result(peer)
            try:
                await peer.wait_closed()
            except TransportClosed:
                pass

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", ready


@asynccontextmanager
async def rpc_pair(methods=None, **peer_options):
    async with rpc_server(methods, **peer_options) as (url, ready):
        transport = WebSocketTransport(url, allow_insecure_localhost=True)
        async with transport.open() as connection:
            async with JsonRpcPeer(connection) as client:
                yield client, await asyncio.wait_for(ready, 3)
