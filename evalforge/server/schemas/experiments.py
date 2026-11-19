"""实验创建参数及查询响应；不暴露 ORM 内部字段。"""

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, JsonValue, StrictBool, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import Required, TypedDict

from .auth import Schema
from .common import PositiveDbId, StoredText, validate_database_json
from .evaluators import EvaluatorConfig, EvaluatorSnapshot

ExperimentStatus = Literal["pending", "running", "cancelling", "completed", "failed", "cancelled"]
JobStatus = Literal["queued", "assigned", "running", "succeeded", "failed", "cancelled", "unknown"]
MAX_EXPERIMENT_JOBS = 10_000


class ExecutionConfig(Schema):
    runner_id: StoredText = Field(min_length=1, max_length=128)
    agent_target_id: StoredText = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(default=60.0, strict=True, gt=0, le=3600, allow_inf_nan=False)
    stream_output: StrictBool = False

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


class ExperimentOptions(Schema):
    name: StoredText = Field(min_length=1, max_length=100)
    description: StoredText | None = Field(default=None, max_length=2000)
    repetitions: int = Field(default=1, strict=True, ge=1, le=100)
    dataset_evaluator_ids: list[PositiveDbId] = Field(default_factory=list, max_length=100)
    evaluators: list[EvaluatorConfig] = Field(default_factory=list, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("dataset_evaluator_ids")
    @classmethod
    def unique_evaluator_ids(cls, ids: list[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise ValueError("Dataset evaluator IDs must be unique")
        return ids

    @model_validator(mode="after")
    def validate_evaluator_selection(self):
        if self.evaluators and self.dataset_evaluator_ids:
            raise ValueError("Use either evaluators or dataset_evaluator_ids")
        if len({evaluator.name for evaluator in self.evaluators}) != len(self.evaluators):
            raise ValueError("Experiment evaluator names must be unique")
        return self


class ExperimentCreate(ExperimentOptions):
    dataset_version_id: PositiveDbId | SkipJsonSchema[None] = Field(default_factory=lambda: None)
    metadata: Annotated[dict[str, JsonValue], AfterValidator(validate_database_json)] = Field(default_factory=dict)
    execution_config: ExecutionConfig

    @field_validator("dataset_version_id")
    @classmethod
    def omit_version_instead_of_null(cls, value):
        if value is None:
            raise ValueError("Omit dataset_version_id to use the latest version")
        return value


class ExperimentConfigSave(ExperimentOptions, ExecutionConfig):
    dataset_id: PositiveDbId
    dataset_version_id: PositiveDbId | None = None


class ExperimentConfigRevision(Schema):
    base_version: PositiveDbId
    config: ExperimentConfigSave


class ExperimentConfigDetail(Schema):
    id: int
    config_group_id: UUID
    version: int
    name: str
    description: str | None
    dataset_id: int
    dataset_version_id: int | None
    runner_id: str
    agent_target_id: str
    timeout_seconds: float
    stream_output: bool
    repetitions: int
    evaluators: list[EvaluatorSnapshot]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ExperimentConfigPage(Schema):
    items: list[ExperimentConfigDetail]
    page: int
    pagesize: int
    total: int


class ExperimentRunCreate(Schema):
    dataset_version_id: PositiveDbId
    example_ids: Annotated[list[PositiveDbId], Field(min_length=1, max_length=MAX_EXPERIMENT_JOBS)] | SkipJsonSchema[None] = Field(
        default_factory=lambda: None,
    )

    @field_validator("example_ids")
    @classmethod
    def unique_examples(cls, ids):
        if ids is None:
            raise ValueError("Omit example_ids to run all examples")
        if len(set(ids)) != len(ids):
            raise ValueError("Example IDs must be unique")
        return ids


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


class EvaluationProgress(Schema):
    expected: int = 0
    completed: int = 0
    errors: int = 0


class ExperimentDetailProgress(ExperimentProgress):
    evaluations: EvaluationProgress = Field(default_factory=EvaluationProgress)


class ExperimentSummary(Schema):
    experiment_id: int = Field(validation_alias="id")
    experiment_config_id: int
    config_group_id: UUID
    config_version: int
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
    # 历史实验可能没有执行目标，只为新增的流式开关提供兼容默认值。
    runner_id: str
    agent_target_id: str
    timeout_seconds: float
    stream_output: Required[bool]


class ExperimentDetail(ExperimentSummary):
    progress: ExperimentDetailProgress
    description: str | None
    metadata: dict[str, JsonValue]
    execution_config: SavedExecutionConfig
    evaluators: list[EvaluatorSnapshot] = Field(default_factory=list)


class ExperimentPage(Schema):
    items: list[ExperimentSummary]
    page: int
    pagesize: int
    total: int


class JobSummary(Schema):
    job_id: str
    example_id: int
    repetition_number: int
    status: JobStatus
    duration_ms: float | None
    http_status: int | None
    evaluation_progress: EvaluationProgress


class JobError(Schema):
    code: str
    message: str


class JobEvaluationDetail(Schema):
    name: str
    score: float | None
    label: str | None
    explanation: str | None
    error: str | None


class JobDetail(JobSummary):
    trace_id: str | None
    input: dict[str, JsonValue]
    reference: dict[str, JsonValue]
    metadata: dict[str, JsonValue]
    output: JsonValue
    error: JobError | None
    started_at: datetime | None
    ended_at: datetime | None
    evaluations: list[JobEvaluationDetail]

    @field_validator("started_at", "ended_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class JobPage(Schema):
    items: list[JobSummary]
    page: int
    pagesize: int
    total: int
