"""单次 Agent HTTP 调用：不调度 Job、不写数据库、不重试、不转换业务字段。"""

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from time import monotonic
from typing import Any

import httpx

from .models import encode_json
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

    async def execute(self, agent_target_id: str, input_json: Any, *, timeout_seconds: float) -> HttpExecutionResult:
        if self._client is None:
            raise RuntimeError("Open the HTTP executor with async with before using it")
        if not isinstance(agent_target_id, str) or agent_target_id not in self._targets:
            raise ValueError("Unknown agent_target_id")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        # content 而非 json=，确保顶层 JSON null 也发送为 null，而不是空请求体。
        payload = encode_json(input_json).encode("utf-8")
        target = self._targets[agent_target_id]
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
                    "POST", target.url, content=payload,
                    headers={**target.headers, "content-type": "application/json", "accept-encoding": "identity"},
                    timeout=timeout_seconds,
                ) as response:
                    status = response.status_code
                    # 不让压缩数据在大小检查之前膨胀；V1 只接收未压缩 JSON。
                    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                        return result(code="unsupported_encoding", message="Compressed Agent responses are not supported")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            return result(code="response_too_large", message="Agent response exceeded the size limit")
                        chunks.append(chunk)
                    try:
                        output = json.loads(b"".join(chunks).decode("utf-8-sig"), parse_constant=_reject_constant)
                        encode_json(output)  # 拒绝数值溢出，保证后续可保存/上报。
                    except (ValueError, UnicodeError, RecursionError):
                        if not 200 <= status < 300:
                            return result(code="http_error", message="Agent returned a non-2xx HTTP status")
                        return result(code="invalid_json", message="Agent response was not valid UTF-8 JSON")
                    if not 200 <= status < 300:
                        return result(output, code="http_error", message="Agent returned a non-2xx HTTP status")
                    return result(output)
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
