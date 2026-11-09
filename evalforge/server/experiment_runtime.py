"""Process-local work buffers for a single experiment."""

from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass, field

from .schemas.evaluators import EvaluatorSnapshot
from .schemas.experiments import ExecutionConfig


@dataclass(frozen=True, slots=True)
class EvalWorkItem:
    experiment_run_id: int
    evaluator_name: str


@dataclass(slots=True)
class ExperimentRuntime:
    """Hold one experiment's configuration and queued work references."""

    experiment_id: int
    execution_config: ExecutionConfig
    evaluators: tuple[EvaluatorSnapshot, ...] = field(default=(), repr=False)
    # Ordered keys give FIFO order and deduplication in the same structure.
    _task_queue: OrderedDict[int, None] = field(default_factory=OrderedDict, init=False, repr=False)
    _eval_queue: OrderedDict[EvalWorkItem, None] = field(default_factory=OrderedDict, init=False, repr=False)

    @property
    def pending_tasks(self) -> int:
        return len(self._task_queue)

    @property
    def pending_evals(self) -> int:
        return len(self._eval_queue)

    def enqueue_task(self, experiment_run_id: int) -> None:
        self._task_queue[experiment_run_id] = None

    def pop_task(self) -> int | None:
        if self._task_queue:
            return self._task_queue.popitem(last=False)[0]
        return None

    def enqueue_evaluations(self, experiment_run_id: int, *, completed_names: Collection[str] = ()) -> None:
        """Buffer this run's configured evaluations after its Task result is committed."""
        for evaluator in self.evaluators:
            item = EvalWorkItem(experiment_run_id, evaluator.name)
            if evaluator.name in completed_names:
                self._eval_queue.pop(item, None)
            else:
                self._eval_queue[item] = None

    def pop_eval(self) -> EvalWorkItem | None:
        if self._eval_queue:
            return self._eval_queue.popitem(last=False)[0]
        return None
