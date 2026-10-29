"""数据契约：样本、Task 执行记录、评分记录分开保存。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


def _new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class Example:
    id: str
    input: dict[str, Any]
    expected: Any
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Example.id must be a non-empty string")
        if not isinstance(self.input, dict) or not isinstance(self.metadata, dict):
            raise ValueError("Example.input and metadata must be objects")


@dataclass(frozen=True)
class Dataset:
    name: str
    examples: list[Example]

    def __post_init__(self) -> None:
        if not self.examples:
            raise ValueError("Dataset must contain at least one example")
        ids = [example.id for example in self.examples]
        if len(ids) != len(set(ids)):
            raise ValueError("Example IDs must be unique within a dataset")


@dataclass(frozen=True)
class EvaluationResult:
    score: float | None = None
    label: str | None = None
    explanation: str | None = None


@dataclass(frozen=True)
class Experiment:
    """一次评测的运行信息；这里只记名称，不保存可执行函数或数据库对象。"""

    name: str
    dataset_name: str
    task_name: str
    evaluator_names: tuple[str, ...]
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class TaskRun:
    example_id: str
    input: dict[str, Any]
    expected: Any
    metadata: dict[str, Any]
    output: Any = None
    error: str | None = None
    experiment_id: str = field(kw_only=True)
    id: str = field(default_factory=_new_id, kw_only=True)


@dataclass(frozen=True)
class EvaluationRun:
    example_id: str
    evaluator: str
    status: Literal["ok", "error", "skipped"]
    result: EvaluationResult | None = None
    error: str | None = None
    task_run_id: str = field(kw_only=True)
    id: str = field(default_factory=_new_id, kw_only=True)


@dataclass(frozen=True)
class ExperimentResult:
    experiment: Experiment
    task_runs: list[TaskRun]
    evaluation_runs: list[EvaluationRun]

    @property
    def experiment_name(self) -> str:
        return self.experiment.name

    @property
    def dataset_name(self) -> str:
        return self.experiment.dataset_name
