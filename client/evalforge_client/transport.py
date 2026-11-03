"""WSS 文本传输；每次重连交出新连接，不恢复会话或重放业务消息。"""

import asyncio
import logging
import math
import random
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect, process_exception
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class TransportClosed(ConnectionError):
    """连接已经断开；RPC 层应终止该连接上的等待。"""


class ConnectionFailed(ConnectionError):
    def __init__(self, *, retryable: bool) -> None:
        super().__init__("WebSocket connection failed" + (" (retryable)" if retryable else " (fatal)"))
        self.retryable = retryable


class WebSocketConnection:
    """一个连接的生命周期；同一时间应只有一个 receive_text 调用者。"""

    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket

    async def send_text(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("Only text messages are supported")
        try:
            await self._socket.send(message)
        except ConnectionClosed:
            raise TransportClosed("WebSocket connection closed") from None

    async def receive_text(self) -> str:
        try:
            message = await self._socket.recv()
        except ConnectionClosed:
            raise TransportClosed("WebSocket connection closed") from None
        if not isinstance(message, str):
            await self._socket.close(code=1003, reason="JSON text required")
            raise TransportClosed("Binary messages are not supported")
        return message

    async def close(self) -> None:
        await self._socket.close()


@dataclass(frozen=True)
class WebSocketTransport:
    url: str = field(repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    allow_insecure_localhost: bool = False
    open_timeout: float = 10
    ping_interval: float = 20
    ping_timeout: float = 20
    close_timeout: float = 5
    max_size: int = 1024 * 1024
    reconnect_min: float = 1
    reconnect_max: float = 30
    stable_after: float = 60

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.url)
            port = parts.port
        except (ValueError, TypeError):
            raise ValueError("Invalid WebSocket URL") from None
        local_ws = (
            self.allow_insecure_localhost and parts.scheme == "ws"
            and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if not parts.hostname or (parts.scheme != "wss" and not local_ws):
            raise ValueError("WSS is required; explicit local development may use loopback WS")
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError("URL credentials and fragments are not supported")
        if port == 0:
            raise ValueError("Invalid WebSocket port")
        if self.bearer_token is not None and (
            not self.bearer_token or "\r" in self.bearer_token or "\n" in self.bearer_token
        ):
            raise ValueError("Invalid bearer token")
        for name in ("open_timeout", "ping_interval", "ping_timeout", "close_timeout",
                     "reconnect_min", "reconnect_max", "stable_after"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if type(self.max_size) is not int or self.max_size <= 0:
            raise ValueError("max_size must be a positive integer")
        if self.reconnect_max < self.reconnect_min:
            raise ValueError("reconnect_max must be >= reconnect_min")

    async def _connect(self) -> WebSocketConnection:
        logger.info("Connecting to Cloud")
        headers = {"Authorization": f"Bearer {self.bearer_token}"} if self.bearer_token else None
        try:
            socket = await connect(
                self.url, additional_headers=headers, open_timeout=self.open_timeout,
                ping_interval=self.ping_interval, ping_timeout=self.ping_timeout,
                close_timeout=self.close_timeout, max_size=self.max_size, max_queue=16,
                # 不隐式读取系统代理；Runner 的 Cloud 通道直接建立 TLS 连接。
                proxy=None,
            )
        except Exception as exc:
            retryable = (
                not isinstance(exc, ssl.SSLCertVerificationError)
                and process_exception(exc) is None
            )
            # 不把可能含 URL / 凭据的底层异常带到调用者或日志。
            logger.log(logging.WARNING if retryable else logging.ERROR,
                       "Cloud connection failed retryable=%s error_type=%s", retryable, type(exc).__name__)
            raise ConnectionFailed(retryable=retryable) from None
        logger.info("Cloud connection established")
        return WebSocketConnection(socket)

    @asynccontextmanager
    async def open(self) -> AsyncIterator[WebSocketConnection]:
        """建立一次连接，退出时关闭；不自动重连。"""
        connection = await self._connect()
        try:
            yield connection
        finally:
            await connection.close()

    async def connections(self) -> AsyncIterator[WebSocketConnection]:
        """带抖动指数退避的新连接流；调用方在每次连接上重建 RPC 和会话。

        调用方须在循环体内处理 TransportClosed；退出时用 contextlib.aclosing
        关闭迭代器，确保当前 socket 立即关闭。认证等永久握手失败直接抛出。
        """
        delay = self.reconnect_min
        retries = 0
        while True:
            try:
                connection = await self._connect()
            except ConnectionFailed as exc:
                if not exc.retryable:
                    raise
            else:
                started = asyncio.get_running_loop().time()
                try:
                    yield connection
                finally:
                    await connection.close()
                    logger.info("Cloud connection closed")
                if asyncio.get_running_loop().time() - started >= self.stable_after:
                    delay = self.reconnect_min
                    retries = 0
            retries += 1
            wait_seconds = random.uniform(delay / 2, delay)
            logger.info("Cloud reconnect scheduled attempt=%d delay_seconds=%.2f", retries, wait_seconds)
            await asyncio.sleep(wait_seconds)
            delay = min(self.reconnect_max, delay * 2)
