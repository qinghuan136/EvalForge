"""Runner WebSocket 入口：握手认证、双向 RPC、连接期间授权检查。"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket
from sqlalchemy.exc import SQLAlchemyError

from ..config import origin_of
from ..errors import APIError, error_response
from ..rpc import JsonRpcPeer
from ..ws_transport import TransportClosed, WebSocketConnection

logger = logging.getLogger(__name__)
router = APIRouter()


async def deny(socket: WebSocket, status: int, code: str, message: str) -> None:
    if "websocket.http.response" in socket.scope.get("extensions", {}):
        await socket.send_denial_response(error_response(status, code, message))
    else:
        # 无 denial 扩展时，ASGI 仍在升级前拒绝，HTTP 状态退为 403。
        await socket.close(code=1008)


@router.websocket("/api/v1/runner/ws")
async def runner_websocket(socket: WebSocket):
    config = socket.app.state.settings
    auth = socket.app.state.runner_auth
    try:
        local_ws = (not config.cookie_secure and socket.client is not None
                    and socket.client.host in {"127.0.0.1", "::1"}
                    and socket.url.hostname in {"localhost", "127.0.0.1", "::1"})
        if socket.scope["scheme"] != "wss" and not local_ws:
            raise APIError(403, "wss_required", "连接需要 WSS")
        if socket.scope.get("query_string"):
            raise APIError(400, "invalid_connection_url", "连接地址不接受查询参数")
        origins = socket.headers.getlist("origin")
        if origins and (len(origins) != 1 or origin_of(origins[0]) != origins[0]
                        or origins[0] not in config.allowed_origins):
            raise APIError(403, "origin_forbidden", "请求来源不受信任")
        headers = socket.headers.getlist("authorization")
        identity = await auth.authenticate(headers[0] if len(headers) == 1 else None)
    except APIError as exc:
        await deny(socket, exc.status, exc.code, exc.message)
        return
    except ValueError:
        await deny(socket, 403, "origin_forbidden", "请求来源不受信任")
        return
    except SQLAlchemyError as exc:
        logger.warning("Runner authentication unavailable error_type=%s", type(exc).__name__)
        await deny(socket, 503, "service_unavailable", "认证服务暂时不可用")
        return

    connection = WebSocketConnection(socket, lambda: auth.check(identity))
    await socket.accept()
    peer = JsonRpcPeer(connection)
    stopping = asyncio.Event()

    async def watch_authorization():
        while True:
            remaining = (identity.expires_at - datetime.now(timezone.utc)).total_seconds()
            delay = min(config.runner_auth_check_seconds, max(0, remaining))
            try:
                await asyncio.wait_for(stopping.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            await connection.check_authorization()

    try:
        # 只提供绑定身份的方法注册入口；业务层后续注册 hello/sync/任务方法。
        setup = socket.app.state.configure_runner_rpc
        if setup is not None:
            setup(peer, identity)
        async with peer:
            closed = asyncio.create_task(peer.wait_closed())
            watcher = asyncio.create_task(watch_authorization())
            tasks = [closed, watcher]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                stopping.set()
                closed.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except TransportClosed:
        pass
    except Exception as exc:
        logger.warning("Runner connection ended error_type=%s", type(exc).__name__)
        await connection.close(1011)
    finally:
        await connection.close()
