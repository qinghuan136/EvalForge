"""单次 Agent HTTP 调用：不调度 Job、不写数据库、不重试、不转换业务字段。"""

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from time import monotonic
from typing import Any

import httpx

from .models import encode_json
from .stream_output import MAX_CHUNK_CHARS, OutputChunk, sse_events
from .targets import AgentTarget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpExecutionResult:
    output: Any = field(repr=False)
    http_status: int | None
    duration_ms: float
    error: dict[str, str] | None = field(repr=False)


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _parse_json(text: str) -> Any:
    output = json.loads(text, parse_constant=_reject_constant)
    encode_json(output).encode("utf-8")  # 拒绝数值溢出和无法保存/上报的孤立代理码位。
    return output


class _ResponseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class HttpExecutor:
    """async with 管理 HTTP 客户端；退出前由调用方先结束执行任务。

    目标配置在构造时快照，URL/请求头不能由 execute 的调用者覆盖。
    timeout_seconds 是覆盖请求及完整响应体读取的总等待时间。
    """

    def __init__(self, targets: dict, *, max_response_bytes: int = 1024 * 1024,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(targets, dict) or any(not isinstance(key, str) or not key.strip() for key in targets):
            raise ValueError("targets must map non-empty target IDs to local configuration")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._targets = {key: AgentTarget.from_config(value) for key, value in targets.items()}
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def has_target(self, agent_target_id: str) -> bool:
        return agent_target_id in self._targets

    async def __aenter__(self) -> "HttpExecutor":
        if self._client is not None:
            raise RuntimeError("HTTP executor is already open")
        client = httpx.AsyncClient(
            transport=self._transport, trust_env=False, follow_redirects=False,
            cookies=CookieJar(policy=_NoCookies()),
        )
        try:
            await client.__aenter__()
        except BaseException:
            await client.aclose()
            raise
        self._client = client
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _response_bytes(self, response: httpx.Response) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise _ResponseError("response_too_large", "Agent response exceeded the size limit")
            yield chunk

    async def _read_json(self, response: httpx.Response) -> Any:
        body = bytearray()
        async for chunk in self._response_bytes(response):
            body.extend(chunk)
        return _parse_json(body.decode("utf-8-sig"))

    async def _read_sse(self, response: httpx.Response, on_chunk: Callable[[OutputChunk], None] | None) -> Any:
        media_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if media_type != "text/event-stream":
            raise _ResponseError("unexpected_content_type", "Agent streaming response must be text/event-stream")
        seq = 0
        async with aclosing(sse_events(self._response_bytes(response))) as events:
            try:
                async for kind, data in events:
                    if kind == "result":
                        try:
                            return _parse_json(data)
                        except (ValueError, RecursionError):
                            raise _ResponseError("invalid_json", "Agent stream result was not valid UTF-8 JSON") from None
                    if kind == "delta" and on_chunk is not None:
                        # 单个 Agent 事件也可能很大，拆成有界片段再交给会话层。
                        for offset in range(0, len(data), MAX_CHUNK_CHARS):
                            seq += 1
                            on_chunk(OutputChunk(seq, data[offset:offset + MAX_CHUNK_CHARS]))
            except UnicodeDecodeError:
                raise _ResponseError("invalid_sse", "Agent event stream was not valid UTF-8") from None
        raise _ResponseError("incomplete_stream", "Agent stream ended before a result event")

    async def execute(self, agent_target_id: str, input_json: Any, *, timeout_seconds: float,
                      stream_output: bool = False,
                      on_chunk: Callable[[OutputChunk], None] | None = None) -> HttpExecutionResult:
        if self._client is None:
            raise RuntimeError("Open the HTTP executor with async with before using it")
        if not isinstance(agent_target_id, str) or agent_target_id not in self._targets:
            raise ValueError("Unknown agent_target_id")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if type(stream_output) is not bool:
            raise ValueError("stream_output must be a boolean")
        # content 而非 json=，确保顶层 JSON null 也发送为 null，而不是空请求体。
        payload = encode_json(input_json).encode("utf-8")
        target = self._targets[agent_target_id]
        headers = {**target.headers, "content-type": "application/json", "accept-encoding": "identity"}
        if stream_output:
            headers["accept"] = "text/event-stream"
        started, status = monotonic(), None
        logger.debug("Agent HTTP request started agent_target_id=%r timeout_seconds=%s", agent_target_id, timeout_seconds)

        def result(output=None, *, code=None, message=None):
            error = None if code is None else {"code": code, "message": message}
            duration_ms = (monotonic() - started) * 1000
            logger.debug("Agent HTTP request finished agent_target_id=%r http_status=%s duration_ms=%.1f error_code=%r",
                         agent_target_id, status, duration_ms, code)
            return HttpExecutionResult(output, status, duration_ms, error)

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client.stream(
                    "POST", target.url, content=payload, headers=headers, timeout=timeout_seconds,
                ) as response:
                    status = response.status_code
                    # 不让压缩数据在大小检查之前膨胀。
                    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                        return result(code="unsupported_encoding", message="Compressed Agent responses are not supported")
                    ok = 200 <= status < 300
                    if stream_output and ok:
                        return result(await self._read_sse(response, on_chunk))
                    try:
                        output = await self._read_json(response)
                    except (ValueError, RecursionError):
                        if ok:
                            return result(code="invalid_json", message="Agent response was not valid UTF-8 JSON")
                        output = None
                    if not ok:
                        return result(output, code="http_error", message="Agent returned a non-2xx HTTP status")
                    return result(output)
        except _ResponseError as exc:
            return result(code=exc.code, message=str(exc))
        except (TimeoutError, httpx.TimeoutException):
            return result(code="timeout", message="Agent HTTP request timed out")
        except httpx.ConnectError:
            return result(code="connection_error", message="Could not connect to Agent")
        except httpx.RequestError:
            return result(code="request_error", message="Agent HTTP request failed")
        except asyncio.CancelledError:
            logger.debug("Agent HTTP wait cancelled agent_target_id=%r duration_ms=%.1f",
                         agent_target_id, (monotonic() - started) * 1000)
            raise  # 继续向上传播；执行层决定如何持久化取消，不能声称 Agent 已停止。
