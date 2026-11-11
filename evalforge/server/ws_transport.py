"""ASGI WebSocket 文本适配；一个读取者、串行发送、限制消息大小。"""

import asyncio

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

MAX_MESSAGE_BYTES = 1024 * 1024


class TransportClosed(ConnectionError):
    """底层连接已结束，当前 RPC 的等待应失败。"""


class WebSocketConnection:
    def __init__(self, socket: WebSocket, *, max_size: int = MAX_MESSAGE_BYTES):
        self.socket, self.max_size = socket, max_size
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def send_text(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("Only text messages are supported")
        await self._check_size(message)
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
