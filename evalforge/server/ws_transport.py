"""ASGI WebSocket 文本适配；一个读取者、串行发送、限制消息大小。"""

import asyncio
from collections.abc import Awaitable, Callable

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from .errors import APIError


class TransportClosed(ConnectionError):
    """底层连接已结束，当前 RPC 的等待应失败。"""


class WebSocketConnection:
    def __init__(self, socket: WebSocket, authorize: Callable[[], Awaitable[None]], *, max_size: int = 1048576):
        self.socket, self.authorize, self.max_size = socket, authorize, max_size
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def check_authorization(self) -> None:
        check = asyncio.create_task(self.authorize())
        try:
            # 断线时让短认证事务完成清理，再传播取消；避免遗留后台连接关闭任务。
            await asyncio.shield(check)
        except asyncio.CancelledError:
            await asyncio.gather(check, return_exceptions=True)
            raise
        except APIError as exc:
            await self.close(1011 if exc.status >= 500 else 1008)
            raise TransportClosed("Runner authorization expired") from None
        except Exception:
            await self.close(1011)
            raise TransportClosed("Runner authorization unavailable") from None

    async def send_text(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("Only text messages are supported")
        await self._check_size(message)
        await self.check_authorization()
        async with self._send_lock:
            if self._closed:
                raise TransportClosed("WebSocket closed")
            try:
                await self.socket.send_text(message)
            except (WebSocketDisconnect, OSError):
                self._closed = True
                raise TransportClosed("WebSocket closed") from None

    async def receive_text(self) -> str:
        if self._closed:
            raise TransportClosed("WebSocket closed")
        try:
            event = await self.socket.receive()
        except (WebSocketDisconnect, OSError):
            self._closed = True
            raise TransportClosed("WebSocket closed") from None
        if event["type"] == "websocket.disconnect":
            self._closed = True
            raise TransportClosed("WebSocket closed")
        text = event.get("text")
        if text is None:
            await self.close(1003)
            raise TransportClosed("JSON text required")
        await self._check_size(text)
        await self.check_authorization()
        return text

    async def _check_size(self, text: str) -> None:
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            await self.close(1007)
            raise TransportClosed("Invalid text") from None
        if size > self.max_size:
            await self.close(1009)
            raise TransportClosed("Message too large")

    async def close(self, code: int = 1000) -> None:
        async with self._send_lock:
            if not self._closed:
                self._closed = True
                if (self.socket.application_state == WebSocketState.CONNECTED
                        and self.socket.client_state != WebSocketState.DISCONNECTED):
                    try:
                        await self.socket.close(code=code)
                    except (WebSocketDisconnect, OSError):
                        pass
