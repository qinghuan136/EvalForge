"""进程内的实验配置、Task 和 Eval 待办。"""

from collections import OrderedDict
from dataclasses import dataclass, field

from pydantic import JsonValue, ValidationError

from .schemas.experiments import ExecutionConfig


@dataclass(frozen=True, slots=True)
class JobWorkItem:
    job_pk: int
    job_id: str
    example_id: int
    input: JsonValue


@dataclass(frozen=True, slots=True)
class EvaluationWorkItem:
    job_pk: int
    name: str
    snapshot: dict[str, JsonValue]
    context: dict[str, JsonValue]

    @property
    def key(self) -> tuple[int, str]:
        return self.job_pk, self.name


@dataclass(slots=True)
class ExperimentRuntime:
    experiment_id: int
    execution_config: ExecutionConfig | None
    evaluators: tuple[dict[str, JsonValue], ...] = field(default=(), repr=False)
    cancelling: bool = False
    completed: bool = False  # 迟到的成功结果可为已完成的实验补评。
    inflight: set[int] = field(default_factory=set, init=False)
    tasks: OrderedDict[int, JobWorkItem] = field(default_factory=OrderedDict, init=False, repr=False)
    evals: dict[tuple[int, str], EvaluationWorkItem] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_saved(cls, experiment_id: int, config: dict, status: str) -> "ExperimentRuntime":
        execution = {key: value for key, value in config.items() if key != "evaluators"}
        try:
            validated = ExecutionConfig.model_validate(execution)
        except ValidationError:
            validated = None  # 旧数据中无效的配置由派发事务记为失败。
        return cls(experiment_id, validated, tuple(config.get("evaluators") or ()),
                   cancelling=status == "cancelling", completed=status == "completed")

    def reserve_task(self) -> JobWorkItem:
        _, item = self.tasks.popitem(last=False)
        self.inflight.add(item.job_pk)
        return item

    def finish_dispatch(self, item: JobWorkItem, *, requeue: bool) -> None:
        self.inflight.discard(item.job_pk)
        if requeue and not self.cancelling:
            self.tasks[item.job_pk] = item
            self.tasks.move_to_end(item.job_pk, last=False)

    def cancel(self) -> None:
        self.cancelling = True
        self.tasks.clear()
        self.evals.clear()
