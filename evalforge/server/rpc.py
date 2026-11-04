"""从现有客户端移植的 jsonrpcserver / jsonrpcclient 单连接双向 RPC 适配。

接收循环仅分流消息。异步方法在受限的后台任务中执行，允许方法内部
反向发起 RPC，同时仍可接收响应和取消指令。
"""

import asyncio
import inspect
import json
import logging
import math
from functools import wraps
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

import jsonrpcclient
from jsonrpcserver import Error, Success, async_dispatch_to_serializable
from jsonrpcserver.response import ErrorResponse, serialize_error
from jsonrpcserver.sentinels import NODATA

from .ws_transport import TransportClosed

logger = logging.getLogger(__name__)
_MISSING = object()
Handler = Callable[..., Awaitable[Any]]


class TextConnection(Protocol):
    async def send_text(self, message: str) -> None: ...
    async def receive_text(self) -> str: ...
    async def close(self) -> None: ...


class RpcError(Exception):
    """远端 error 响应，或处理函数主动报告的 RPC 错误。"""

    def __init__(self, code: int, message: str, data: Any = _MISSING) -> None:
        if type(code) is not int or not isinstance(message, str):
            raise ValueError("RPC error requires an integer code and a string message")
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class RpcProtocolError(Exception):
    """对应本地请求的响应格式无效。"""


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _error(code: int, message: str) -> dict:
    return serialize_error(ErrorResponse(code, message, NODATA, None))


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _decode(text: str) -> Any:
    value = json.loads(text, parse_constant=_reject_constant)
    _encode(value)  # 拒绝 1e999 溢出；JSON-RPC 不能传递非有限数。
    return value


class JsonRpcPeer:
    """通过 async with 启动/关闭；一个实例仅绑定一条连接。

    注册方法必须是 async 函数。call 的超时只终止本地等待，不撤销远端执行。
    """

    def __init__(
        self, connection: TextConnection, *, max_pending: int = 128,
        max_handlers: int = 32, max_batch: int = 32,
    ) -> None:
        for value in (max_pending, max_handlers, max_batch):
            if type(value) is not int or value <= 0:
                raise ValueError("RPC capacity limits must be positive integers")
        self._connection = connection
        self._max_pending, self._max_handlers, self._max_batch = max_pending, max_handlers, max_batch
        self._methods: dict[str, Handler] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: set[asyncio.Task] = set()
        self._executing: set[asyncio.Task] = set()
        self._reader: asyncio.Task | None = None
        self._closed = False

    def register(self, method: str, handler: Handler) -> None:
        self._validate_method(method)
        if method in self._methods:
            raise ValueError("Method already registered")
        if not (inspect.iscoroutinefunction(handler) or
                inspect.iscoroutinefunction(getattr(handler, "__call__", None))):
            raise ValueError("RPC handlers must be async functions")
        @wraps(handler)
        async def adapted(*args, **kwargs):
            # jsonrpcserver 用子任务调用方法；记录它们以支持方法内部关闭 Peer。
            task = asyncio.current_task()
            self._executing.add(task)
            try:
                try:
                    value = await handler(*args, **kwargs)
                except RpcError as exc:
                    if exc.data is not _MISSING:
                        _encode(exc.data)
                    return Error(exc.code, exc.message, NODATA if exc.data is _MISSING else exc.data)
                _encode(value)
                return Success(value)
            except Exception:
                # 5.0.9 默认附带异常文本；在适配边界统一脱敏。
                return Error(-32603, "Internal error")
            finally:
                self._executing.discard(task)

        self._methods[method] = adapted

    @staticmethod
    def _validate_method(method: str) -> None:
        if not isinstance(method, str) or not method or method.startswith("rpc."):
            raise ValueError("Method must be non-empty and outside the reserved rpc. namespace")

    def _ensure_open(self) -> None:
        if self._reader is None:
            raise RuntimeError("Start the peer with async with before sending messages")
        if self._closed or self._reader.done():
            raise TransportClosed("RPC connection closed")

    @staticmethod
    def _params(method: str, params: Any) -> Any:
        JsonRpcPeer._validate_method(method)
        if params is not _MISSING:
            if not isinstance(params, (dict, list)):
                raise ValueError("params must be an object or array")
            return params
        return None

    async def call(self, method: str, params: Any = _MISSING, *, timeout: float = 30) -> Any:
        self._ensure_open()
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if len(self._pending) >= self._max_pending:
            raise RuntimeError("Too many pending RPC calls")
        message = jsonrpcclient.request(method, self._params(method, params), id=uuid4().hex)
        request_id = message["id"]
        encoded = _encode(message)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self._connection.send_text(encoded)
                return await future
        except (TimeoutError, TransportClosed, RpcError, RpcProtocolError) as exc:
            logger.debug("RPC call failed method=%r request_id=%r error_type=%s",
                         method, request_id, type(exc).__name__)
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # send 与断线可能同时发生；取走异常，避免未读取的 Future 警告。
                future.exception()

    async def notify(self, method: str, params: Any = _MISSING) -> None:
        self._ensure_open()
        await self._connection.send_text(_encode(jsonrpcclient.notification(method, self._params(method, params))))

    async def __aenter__(self) -> "JsonRpcPeer":
        if self._reader is not None:
            raise RuntimeError("An RPC peer cannot be restarted")
        self._reader = asyncio.create_task(self._receive(), name="json-rpc-receiver")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def wait_closed(self) -> None:
        if self._reader is None:
            raise RuntimeError("RPC peer has not started")
        await asyncio.shield(self._reader)

    async def close(self) -> None:
        if self._reader is None:
            return
        if asyncio.current_task() in self._handlers | self._executing:
            # 接收循环退出时会取消并等待处理函数；处理函数不能反过来等待它。
            await self._connection.close()
            return
        try:
            await self._connection.close()
        finally:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    async def _receive(self) -> None:
        try:
            while True:
                text = await self._connection.receive_text()
                try:
                    message = _decode(text)
                except (ValueError, TypeError, RecursionError):
                    # Parse error 的构造仍交给库，使用相同的严格 JSON 解码器。
                    reply = await async_dispatch_to_serializable(text, methods={}, deserializer=_decode)
                    reply["error"].pop("data", None)
                    await self._connection.send_text(_encode(reply))
                    continue
                batch = isinstance(message, list)
                if batch and (not message or len(message) > self._max_batch):
                    await self._connection.send_text(_encode(_error(-32600, "Invalid Request")))
                    continue
                requests = []
                for item in message if batch else [message]:
                    # 响应立即匹配，不能等处理函数完成；否则反向调用会死锁。
                    if isinstance(item, dict) and "method" not in item and (
                        "result" in item or "error" in item or
                        (isinstance(item.get("id"), str) and item["id"] in self._pending)
                    ):
                        self._resolve(item)
                    else:
                        requests.append(item)
                if requests:
                    if len(self._handlers) >= self._max_handlers:
                        await self._answer(requests, batch, overloaded=True)
                    else:
                        task = asyncio.create_task(self._answer(requests, batch))
                        self._handlers.add(task)
                        task.add_done_callback(self._handler_done)
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TransportClosed("RPC connection closed"))
            handlers = list(self._handlers)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    def _handler_done(self, task: asyncio.Task) -> None:
        self._handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("RPC reply could not be delivered error_type=%s", type(task.exception()).__name__)

    def _resolve(self, message: dict) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id) if isinstance(request_id, str) else None
        if future is None or future.done():
            return  # 迟到、重复或未知响应不匹配到其他调用，也不产生响应循环。
        try:
            # jsonrpcclient 4.x 对版本和互斥字段宽松；补上严格的 2.0 边界。
            if message.get("jsonrpc") != "2.0" or (("result" in message) == ("error" in message)):
                raise ValueError("Invalid response envelope")
            parsed = jsonrpcclient.parse(message)
            if isinstance(parsed, jsonrpcclient.Error):
                if type(parsed.code) is not int or not isinstance(parsed.message, str):
                    raise ValueError("Invalid error fields")
                future.set_exception(RpcError(parsed.code, parsed.message, parsed.data))
            else:
                future.set_result(parsed.result)
        except (ValueError, TypeError, KeyError, AttributeError):
            future.set_exception(RpcProtocolError("Invalid JSON-RPC response"))

    async def _answer(self, requests: list, batch: bool, *, overloaded: bool = False) -> None:
        methods = self._methods
        if overloaded:
            async def reject(*args, **kwargs):
                return Error(-32000, "RPC capacity exceeded")
            methods = {name: reject for name in self._methods}
        replies = []
        for request in requests:
            # 逐条交给库，保持批处理容量有界，避免库一次启动整批处理子任务。
            # 外包一层 batch，让库把每个元素校验为单个请求；嵌套数组不能再次当 batch 执行。
            response = await async_dispatch_to_serializable(_encode([request]), methods=methods)
            reply = response[0] if isinstance(response, list) else response
            if reply is not None:
                replies.append(reply)
        if replies:
            await self._connection.send_text(_encode(replies if batch else replies[0]))
