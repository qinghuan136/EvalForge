"""云端数据集导入和查询的接口结构。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from .auth import Schema
from .common import PositiveDbId, StoredText

MAX_DATASET_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DATASET_EXAMPLES = 10_000
MAX_CSV_COLUMNS = 256


class DatasetImportOptions(Schema):
    name: StoredText = Field(min_length=1, max_length=100)
    description: StoredText | None = Field(default=None, max_length=2000)
    format: Literal["csv", "jsonl"]
    input_keys: list[str] = Field(default_factory=list)
    output_keys: list[str] = Field(default_factory=list)
    metadata_keys: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class DatasetImportResult(Schema):
    dataset_id: PositiveDbId
    dataset_version_id: PositiveDbId
    example_count: int


class DatasetSummary(Schema):
    dataset_id: int = Field(validation_alias="id")
    name: str
    description: str | None
    latest_version_id: int | None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DatasetDetail(DatasetSummary):
    metadata: dict[str, JsonValue]
    example_count: int


class DatasetPage(Schema):
    items: list[DatasetSummary]
    page: int
    pagesize: int
    total: int


class DatasetExampleResponse(Schema):
    example_id: int
    input: dict[str, JsonValue]
    output: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


class DatasetExamplePage(Schema):
    dataset_id: int
    dataset_version_id: int | None
    items: list[DatasetExampleResponse]
    page: int
    pagesize: int
    total: int
