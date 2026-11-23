"""浏览器端自定义 Python 评分器管理契约。"""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, FiniteFloat, JsonValue, field_validator, model_validator

from .auth import Schema
from .common import StoredText, validate_database_json


def trim_name(value):
    return value.strip() if isinstance(value, str) else value


EvaluatorName = Annotated[StoredText, BeforeValidator(trim_name), Field(min_length=1, max_length=100)]
EvaluatorDescription = Annotated[StoredText, Field(max_length=2000)]


def _utc_time(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CodeEvaluatorCreate(Schema):
    name: EvaluatorName
    description: EvaluatorDescription | None = None


class CodeEvaluatorPatch(Schema):
    name: EvaluatorName | None = None
    description: EvaluatorDescription | None = None

    @model_validator(mode="after")
    def check_changes(self):
        if not self.model_fields_set or ("name" in self.model_fields_set and self.name is None):
            raise ValueError("Provide a valid name or description")
        return self


class CodeEvaluatorDraft(Schema):
    source_code: StoredText = Field(max_length=100_000)


class CodeEvaluatorDraftSaved(Schema):
    draft_source_code: str


class CodeEvaluatorContext(Schema):
    input: dict[str, JsonValue]
    reference: dict[str, JsonValue]
    output: JsonValue
    metadata: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_json_values(self):
        validate_database_json(self.model_dump())
        return self


class CodeEvaluatorPreviewRequest(CodeEvaluatorDraft):
    context: CodeEvaluatorContext

    @field_validator("source_code")
    @classmethod
    def require_source(cls, source: str) -> str:
        if not source.strip():
            raise ValueError("Source code must not be blank")
        return source


class CodeEvaluatorPreviewResult(Schema):
    score: Annotated[FiniteFloat, Field(strict=True)] | None = None
    label: StoredText | None = Field(default=None, max_length=512)
    explanation: StoredText | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def require_score_or_label(self):
        if self.score is None and self.label is None:
            raise ValueError("An evaluator must return score or label")
        return self


class CodeEvaluatorPreviewOutcome(Schema):
    simulated: Literal[False] = False
    duration_ms: int
    stdout: str = ""


class CodeEvaluatorPreviewSucceeded(CodeEvaluatorPreviewOutcome):
    status: Literal["succeeded"] = "succeeded"
    result: CodeEvaluatorPreviewResult


class CodeEvaluatorPreviewError(Schema):
    code: str
    message: str


class CodeEvaluatorPreviewFailed(CodeEvaluatorPreviewOutcome):
    status: Literal["failed"] = "failed"
    result: None = None
    error: CodeEvaluatorPreviewError


CodeEvaluatorPreview = Annotated[CodeEvaluatorPreviewSucceeded | CodeEvaluatorPreviewFailed, Field(discriminator="status")]


class CodeEvaluatorVersionSummary(Schema):
    id: int
    version_number: int
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return _utc_time(value)


class CodeEvaluatorVersionDetail(CodeEvaluatorVersionSummary):
    source_code: str


class CodeEvaluatorSummary(Schema):
    id: int
    name: str
    description: str | None
    latest_version: CodeEvaluatorVersionSummary | None
    archived_at: datetime | None

    @field_validator("archived_at")
    @classmethod
    def normalize_archived_at(cls, value: datetime | None) -> datetime | None:
        return _utc_time(value) if value is not None else None


class CodeEvaluatorDetail(CodeEvaluatorSummary):
    draft_source_code: str | None


class CodeEvaluatorPage(Schema):
    items: list[CodeEvaluatorSummary]
    page: int
    pagesize: int
    total: int


class CodeEvaluatorVersionPage(Schema):
    items: list[CodeEvaluatorVersionSummary]
    page: int
    pagesize: int
    total: int
