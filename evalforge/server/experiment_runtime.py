"""Process-local work buffers for a single experiment."""

from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass, field

from .schemas.evaluators import EvaluatorSnapshot
from .schemas.experiments import ExecutionConfig


@dataclass(frozen=True, slots=True)
class EvaluationWorkKey:
    job_pk: int
    evaluator_name: str


@dataclass(slots=True)
class ExperimentRuntime:
    """Hold one experiment's configuration and queued work references."""

    experiment_id: int
    execution_config: ExecutionConfig
    evaluators: tuple[EvaluatorSnapshot, ...] = field(default=(), repr=False)
    # Ordered keys give FIFO order and deduplication in the same structure.
    _task_queue: OrderedDict[int, None] = field(default_factory=OrderedDict, init=False, repr=False)
    _eval_queue: OrderedDict[EvaluationWorkKey, None] = field(default_factory=OrderedDict, init=False, repr=False)

    @property
    def pending_tasks(self) -> int:
        return len(self._task_queue)

    @property
    def pending_evals(self) -> int:
        return len(self._eval_queue)

    def enqueue_task(self, job_pk: int) -> None:
        self._task_queue[job_pk] = None

    def pop_task(self) -> int | None:
        if self._task_queue:
            return self._task_queue.popitem(last=False)[0]
        return None

    def enqueue_evaluations(self, job_pk: int, *, completed_names: Collection[str] = ()) -> None:
        """Buffer this Job's configured evaluations after its Task result is committed."""
        for evaluator in self.evaluators:
            item = EvaluationWorkKey(job_pk, evaluator.name)
            if evaluator.name in completed_names:
                self._eval_queue.pop(item, None)
            else:
                self._eval_queue[item] = None

    def pop_eval(self) -> EvaluationWorkKey | None:
        if self._eval_queue:
            return self._eval_queue.popitem(last=False)[0]
        return None
