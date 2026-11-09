"""平台规则目录、数据集评分配置和实验评分快照的接口契约。"""

from typing import Annotated

from pydantic import AfterValidator, Field, JsonValue, field_validator

from .auth import Schema
from .common import PositiveDbId, StoredText, validate_database_json


class BuiltinEvaluatorDefinition(Schema):
    key: str
    description: str
    input_schema: dict[str, JsonValue]
    output_configs: list[dict[str, JsonValue]]


class InputMapping(Schema):
    path_mapping: dict[str, str] = Field(default_factory=dict)
    literal_mapping: Annotated[dict[str, JsonValue], AfterValidator(validate_database_json)] = Field(default_factory=dict)


class DatasetEvaluatorCreate(Schema):
    name: StoredText = Field(min_length=1, max_length=100)
    evaluator_key: str = Field(min_length=1, max_length=100)
    description: StoredText | None = Field(default=None, max_length=2000)
    input_mapping: InputMapping

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class EvaluatorSnapshot(DatasetEvaluatorCreate):
    dataset_evaluator_id: PositiveDbId
    output_configs: list[dict[str, JsonValue]]


class DatasetEvaluatorDetail(EvaluatorSnapshot):
    dataset_id: PositiveDbId


class DatasetEvaluatorPage(Schema):
    items: list[DatasetEvaluatorDetail]
    page: int
    pagesize: int
    total: int
