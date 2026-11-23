"""Execute Python evaluators via Snekbox's HTTP API."""

import asyncio
import base64
import json
from time import monotonic

import httpx
from pydantic import BaseModel, StrictInt, ValidationError

from ..errors import APIError
from ..schemas.code_evaluators import (
    CodeEvaluatorContext, CodeEvaluatorPreview, CodeEvaluatorPreviewError, CodeEvaluatorPreviewFailed,
    CodeEvaluatorPreviewResult, CodeEvaluatorPreviewSucceeded,
)

CONTEXT_LIMIT = 256 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
RESULT_LIMIT = 32 * 1024
STDOUT_LIMIT = 8_192
REQUEST_DEADLINE = 15
RESULT_PATH = "result.json"

# This script is sent to Snekbox as a file and runs inside the jail.
ENTRYPOINT = b"""import json
from pathlib import Path

from evaluator import evaluate

context = json.loads(Path("context.json").read_text(encoding="utf-8"))
result = evaluate(context)
try:
    encoded = json.dumps(result, allow_nan=False)
except (TypeError, ValueError, RecursionError):
    encoded = "null"
Path("result.json").write_text(encoded, encoding="utf-8")
"""


class _Attachment(BaseModel):
    path: str
    content: str


class _Reply(BaseModel):
    returncode: StrictInt | None
    stdout: str
    files: list[_Attachment]


def _file(path: str, contents: bytes) -> dict[str, str]:
    return {"path": path, "content": base64.b64encode(contents).decode("ascii")}


def _unavailable() -> APIError:
    return APIError(503, "sandbox_unavailable", "评分沙箱暂时不可用")


def _parse_reply(reply: _Reply, duration_ms: int) -> CodeEvaluatorPreview:
    if reply.returncode in (None, 255):
        raise _unavailable()

    stdout = reply.stdout[:STDOUT_LIMIT]
    if reply.returncode == 137:
        code, message = "sandbox_resource_limit", "代码执行超过沙箱资源限制"
    elif reply.returncode != 0:
        code, message = "evaluator_execution_failed", "Python 评分代码执行失败"
    else:
        results = [file.content for file in reply.files if file.path == RESULT_PATH]
        if len(results) == 1 and len(results[0]) <= 4 * ((RESULT_LIMIT + 2) // 3):
            try:
                result = base64.b64decode(results[0], validate=True)
                if len(result) <= RESULT_LIMIT:
                    parsed = CodeEvaluatorPreviewResult.model_validate_json(result)
                    return CodeEvaluatorPreviewSucceeded(result=parsed, duration_ms=duration_ms, stdout=stdout)
            except (ValueError, ValidationError):
                pass
        code, message = "invalid_evaluator_result", "返回值须为 JSON 对象，并提供有效的 score 或 label"

    return CodeEvaluatorPreviewFailed(error=CodeEvaluatorPreviewError(code=code, message=message),
                                      duration_ms=duration_ms, stdout=stdout)


class SnekboxClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def execute(self, source_code: str, context: CodeEvaluatorContext) -> CodeEvaluatorPreview:
        context_json = context.model_dump_json().encode("utf-8")
        if len(context_json) > CONTEXT_LIMIT:
            raise APIError(422, "invalid_request", "评分 context 的 JSON 大小不能超过 256 KiB")
        payload = {"args": ["main.py"], "files": [
            _file("main.py", ENTRYPOINT),
            _file("evaluator.py", source_code.encode("utf-8")),
            _file("context.json", context_json),
        ]}
        started = monotonic()
        body = bytearray()
        try:
            async with asyncio.timeout(REQUEST_DEADLINE):
                async with self.http.stream("POST", "/eval", json=payload) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > RESPONSE_LIMIT:
                            raise _unavailable()
                        body.extend(chunk)
            reply = _Reply.model_validate_json(body)
        except (TimeoutError, httpx.HTTPError, ValidationError):
            raise _unavailable() from None
        return _parse_reply(reply, int((monotonic() - started) * 1000))
