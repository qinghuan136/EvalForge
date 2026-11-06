"""Runner 上报协议；JSON-RPC id 不属于业务参数。"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Identity = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class AttemptKey(Message):
    job_id: Identity
    attempt_id: Identity


class ResultError(Message):
    code: Annotated[str, Field(min_length=1, max_length=128)]
    message: Annotated[str, Field(max_length=4096)]


class AttemptResult(AttemptKey):
    status: Literal["succeeded", "failed", "cancelled"]
    output: JsonValue
    duration_ms: Annotated[float, Field(ge=0)]
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    error: ResultError | None

    @model_validator(mode="after")
    def valid_result(self):
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("Successful result cannot contain an error")
        if self.status == "failed" and self.error is None:
            raise ValueError("Failed result requires an error")
        # JsonValue 的嵌套浮点数也必须是合法 JSON，不能把 NaN 写入 JSONB。
        json.dumps(self.output, allow_nan=False)
        return self


class ActiveAttempt(AttemptKey):
    status: Literal["accepted", "running", "unknown"]


class RunnerSync(Message):
    runner_id: Identity
    active_jobs: Annotated[list[ActiveAttempt], Field(max_length=1)]
    pending_results: Annotated[list[AttemptResult], Field(max_length=128)]

    @model_validator(mode="after")
    def unique_attempts(self):
        rows = [*self.active_jobs, *self.pending_results]
        if len({(row.job_id, row.attempt_id) for row in rows}) != len(rows):
            raise ValueError("Duplicate attempt in sync snapshot")
        return self


class RunnerReady(Message):
    session_id: Identity
