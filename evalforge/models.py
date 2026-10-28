"""数据契约：样本、Task 执行记录、评分记录分开保存。"""

from dataclasses import dataclass, field
from typing import Any, Literal


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
class TaskRun:
    example_id: str
    input: dict[str, Any]
    expected: Any
    metadata: dict[str, Any]
    output: Any = None
    error: str | None = None


@dataclass(frozen=True)
class EvaluationRun:
    example_id: str
    evaluator: str
    status: Literal["ok", "error", "skipped"]
    result: EvaluationResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class ExperimentResult:
    experiment_name: str
    dataset_name: str
    task_runs: list[TaskRun]
    evaluation_runs: list[EvaluationRun]
