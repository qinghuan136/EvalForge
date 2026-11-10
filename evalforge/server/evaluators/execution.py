"""Execute built-in rules without holding the cloud event loop."""

import asyncio
import json
import sys
from dataclasses import asdict

from pydantic import JsonValue

from evalforge.models import EvaluationResult

from .builtin import get_builtin_evaluator

EVALUATION_TIMEOUT_SECONDS = 30


def _evaluate(key: str, inputs: dict[str, JsonValue]) -> tuple[EvaluationResult, str | None]:
    try:
        return get_builtin_evaluator(key).evaluate(inputs), None
    except Exception:
        return EvaluationResult(), "评分器或输入无效"


async def evaluate_builtin(key: str, inputs: dict[str, JsonValue]) -> tuple[EvaluationResult, str | None]:
    if key not in {"regex", "levenshtein_distance"}:
        return await asyncio.to_thread(_evaluate, key, inputs)

    # Regex backtracking and edit distance can run for a long time. Run them in a
    # process that can be terminated on timeout or scheduler shutdown.
    request = json.dumps([key, inputs]).encode("ascii")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", __name__, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(EVALUATION_TIMEOUT_SECONDS):
            output, _ = await process.communicate(request)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode != 0:
        raise RuntimeError("Built-in evaluator process failed")
    result, error = json.loads(output)
    return EvaluationResult(**result), error


if __name__ == "__main__":
    result, error = _evaluate(*json.load(sys.stdin))
    json.dump((asdict(result), error), sys.stdout)
