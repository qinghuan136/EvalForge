"""云端数据集导入的表单参数和响应。"""

from typing import Literal

from pydantic import Field, field_validator

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
