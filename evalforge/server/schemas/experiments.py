"""实验创建参数及查询响应；不暴露 ORM 内部字段。"""

import math
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, JsonValue, field_validator
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import TypedDict

from .auth import Schema

MAX_DB_ID = 2_147_483_647
PositiveDbId = Annotated[int, Field(strict=True, ge=1, le=MAX_DB_ID)]
ExperimentStatus = Literal["pending", "running", "cancelling", "completed", "failed", "cancelled"]


def database_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Text must not contain a null character")
    value.encode("utf-8")  # PostgreSQL 文本与 JSONB 不能保存孤立代理字符。
    return value


StoredText = Annotated[str, AfterValidator(database_text)]


class ExecutionConfig(Schema):
    runner_id: StoredText = Field(min_length=1, max_length=128)
    agent_target_id: StoredText = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(default=60.0, strict=True, gt=0, le=3600, allow_inf_nan=False)

    @field_validator("runner_id")
    @classmethod
    def nonblank_runner_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Runner ID must not be blank")
        return value

    @field_validator("agent_target_id", mode="before")
    @classmethod
    def trim_target(cls, value):
        return value.strip() if isinstance(value, str) else value


class ExperimentCreate(Schema):
    name: StoredText = Field(min_length=1, max_length=100)
    description: StoredText | None = Field(default=None, max_length=2000)
    dataset_version_id: PositiveDbId | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    repetitions: int = Field(default=1, strict=True, ge=1, le=100)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    execution_config: ExecutionConfig

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("dataset_version_id")
    @classmethod
    def omit_version_instead_of_null(cls, value):
        if value is None:
            raise ValueError("Omit dataset_version_id to use the latest version")
        return value

    @field_validator("metadata")
    @classmethod
    def valid_metadata(cls, value):
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str):
                database_text(item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Metadata numbers must be finite")
        return value


class ExperimentProgress(Schema):
    example_count: int = 0
    job_count: int = 0
    queued: int = 0
    assigned: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    unknown: int = 0


class ExperimentSummary(Schema):
    experiment_id: int = Field(validation_alias="id")
    dataset_id: int
    dataset_version_id: int
    name: str
    status: ExperimentStatus
    repetitions: int
    progress: ExperimentProgress
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SavedExecutionConfig(TypedDict, total=False):
    # 历史实验允许没有配置，查询时不凭空填默认值。
    runner_id: str
    agent_target_id: str
    timeout_seconds: float


class ExperimentDetail(ExperimentSummary):
    description: str | None
    metadata: dict[str, JsonValue]
    execution_config: SavedExecutionConfig


class ExperimentPage(Schema):
    items: list[ExperimentSummary]
    page: int
    pagesize: int
    total: int
