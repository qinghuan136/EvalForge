"""从本进程的 Runtime 调度 Task 和 Eval；主循环不访问外部存储。"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import logging

from .experiment_runtime import EvaluationWorkItem, ExperimentRuntime, JobWorkItem
from .runner_dispatch import send_cancel, send_dispatch
from .runner_sessions import RunnerSession
from .schemas.attempts import AttemptKey
from .services.attempts import AttemptCompletion
from .services.dispatch import ActiveAssignment, DispatchClaim, DispatchService
from .services.evaluations import EvaluationService
from .services.experiment_lifecycle import ACTIVE_EXPERIMENTS
from .services.runtime_loader import RuntimeLoader

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunnerWork:
    session: RunnerSession
    runtimes: dict[int, ExperimentRuntime] = field(default_factory=dict)
    task_candidates: OrderedDict[int, ExperimentRuntime] = field(default_factory=OrderedDict)
    loading: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: bool = False
    active: AttemptKey | None = None
    completed_during_dispatch: set[str] = field(default_factory=set)
    check_cancel: bool = False
    cancel_ack: str | None = None
    task_retry: asyncio.TimerHandle | None = None
    cancel_retry: asyncio.TimerHandle | None = None

    def next_task(self) -> ExperimentRuntime | None:
        if self.active is not None or self.task_retry is not None:
            return None
        return next(iter(self.task_candidates.values()), None)

    def update_candidate(self, runtime: ExperimentRuntime, *, first: bool = False) -> None:
        experiment_id = runtime.experiment_id
        config = runtime.execution_config
        if (self.runtimes.get(experiment_id) is runtime and runtime.tasks and not runtime.cancelling
                and (config is None or not config.stream_output or self.session.supports_streaming)):
            self.task_candidates[experiment_id] = runtime
            if first:
                self.task_candidates.move_to_end(experiment_id, last=False)
        else:
            self.task_candidates.pop(experiment_id, None)


class Scheduler:
    def __init__(self, dispatch: DispatchService, loader: RuntimeLoader, evaluations: EvaluationService, *,
                 dispatch_timeout: float = 5, retry_delay: float = 5):
        self.dispatch, self.loader, self.evaluations = dispatch, loader, evaluations
        self.dispatch_timeout, self.retry_delay = dispatch_timeout, retry_delay
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop: asyncio.Task | None = None
        self._runners: dict[str, RunnerWork] = {}
        self._task_ready: dict[str, None] = {}
        self._eval_ready: dict[tuple[str, int], None] = {}
        self._dispatches: dict[str, asyncio.Task[None]] = {}
        self._dispatch_slots = asyncio.Semaphore(32)
        self._eval_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run(), name="scheduler")

    async def close(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop is not None:
            await self._loop
        for runner in list(self._runners.values()):
            self.session_closed(runner.session)
        evaluations = tuple(self._eval_tasks.values())
        for task in evaluations:
            task.cancel()
        await asyncio.gather(*self._dispatches.values(), *evaluations, return_exceptions=True)

    def _current(self, runner: RunnerWork) -> bool:
        return (not self._stopping and runner.session.can_dispatch
                and self._runners.get(runner.session.identity.runner_id) is runner)

    async def session_ready(self, session: RunnerSession) -> None:
        if self._stopping or not session.can_dispatch:
            return
        runner_id = session.identity.runner_id
        if (old := self._runners.get(runner_id)) is not None:
            if old.session is session:
                return
            self.session_closed(old.session)
        runner = self._runners[runner_id] = RunnerWork(session)
        async with runner.loading:
            snapshot = await self.loader.load(session.identity)
            await session.check_current()
            if not self._current(runner):
                return
            if snapshot.active is not None:
                runner.active = snapshot.active.key
                runner.check_cancel = snapshot.active.cancel_reason is not None
            runner.ready = True
            for runtime in snapshot.experiments.values():
                self._register(runner, runtime)
            self._signal_task(runner)

    def session_closed(self, session: RunnerSession) -> None:
        runner_id = session.identity.runner_id
        runner = self._runners.get(runner_id)
        if runner is None or runner.session is not session:
            return
        self._runners.pop(runner_id)
        self._task_ready.pop(runner_id, None)
        for experiment_id in runner.runtimes:
            self._eval_ready.pop((runner_id, experiment_id), None)
        for timer in (runner.task_retry, runner.cancel_retry):
            if timer is not None:
                timer.cancel()
        # 已启动的派发持有自己的 runtime 引用，可以独立收尾。
        runner.runtimes.clear()
        runner.task_candidates.clear()

    async def experiment_created(self, runner_id: str, experiment_id: int) -> None:
        runner = self._runners.get(runner_id)
        if runner is None:
            return
        try:
            async with runner.loading:
                await self._load_one(runner, experiment_id)
        except Exception as exc:
            logger.error("Runtime load failed experiment_id=%s error_type=%s", experiment_id, type(exc).__name__)
            runner.session.request_close(1011)  # 实验已提交；在新会话恢复。

    async def _load_one(self, runner: RunnerWork, experiment_id: int) -> None:
        if not self._current(runner) or not runner.ready or experiment_id in runner.runtimes:
            return
        snapshot = await self.loader.load(runner.session.identity, experiment_id=experiment_id)
        if self._current(runner) and (runtime := snapshot.experiments.get(experiment_id)) is not None:
            self._register(runner, runtime)

    def _register(self, runner: RunnerWork, runtime: ExperimentRuntime) -> None:
        # A local Eval may have survived the old connection and still be writing its result.
        for key in runtime.evals.keys() & self._eval_tasks.keys():
            runtime.evals.pop(key)
        if runtime.completed and not runtime.evals:
            return
        runner.runtimes[runtime.experiment_id] = runtime
        runner.update_candidate(runtime)
        self._signal_eval(runner, runtime)
        self._signal_task(runner)

    async def experiment_cancelled(self, runner_id: str, experiment_id: int, status: str) -> None:
        runner = self._runners.get(runner_id)
        if runner is None:
            return
        async with runner.loading:
            if self._current(runner):
                self._apply_status(runner, experiment_id, status)
                if status == "cancelling":
                    runner.check_cancel = True
                    self._signal_task(runner)

    async def task_completed(self, session: RunnerSession, key: AttemptKey,
                             completion: AttemptCompletion | None) -> None:
        runner_id = session.identity.runner_id
        runner = self._runners.get(runner_id)
        if runner is None or runner.session is not session or not self._current(runner):
            return  # syncing 时补传的结果在 ready 加载中统一发现。
        if runner_id in self._dispatches:
            runner.completed_during_dispatch.add(key.attempt_id)
        if runner.active is not None and runner.active.attempt_id == key.attempt_id:
            runner.active = None
            if runner.cancel_retry is not None:
                runner.cancel_retry.cancel()
                runner.cancel_retry = None
        if completion is not None:
            experiment_id = completion.experiment_id
            if completion.evaluations:
                runtime = runner.runtimes.get(experiment_id)
                if runtime is None:
                    # unknown 后迟到的成功结果可能让已收尾的实验重新进入评分。
                    await self.experiment_created(runner_id, experiment_id)
                elif not runtime.cancelling:
                    runtime.evals.update((item.key, item) for item in completion.evaluations)
                    self._signal_eval(runner, runtime)
            else:
                self._apply_status(runner, experiment_id, completion.status)
        self._signal_task(runner)

    def _apply_status(self, runner: RunnerWork, experiment_id: int, status: str | None) -> None:
        runtime = runner.runtimes.get(experiment_id)
        if runtime is None or status is None:
            return
        if status == "cancelling":
            runtime.cancel()
        elif status not in ACTIVE_EXPERIMENTS:
            runtime.cancel()
            runner.runtimes.pop(experiment_id)
            self._eval_ready.pop((runner.session.identity.runner_id, experiment_id), None)
        runner.update_candidate(runtime)

    def _signal_task(self, runner: RunnerWork) -> None:
        if (self._current(runner) and runner.ready
                and (runner.next_task() is not None or (runner.check_cancel and runner.cancel_retry is None))):
            self._task_ready[runner.session.identity.runner_id] = None
            self._wake.set()

    def _signal_eval(self, runner: RunnerWork, runtime: ExperimentRuntime) -> None:
        if self._current(runner) and runtime.evals and not runtime.cancelling:
            self._eval_ready[(runner.session.identity.runner_id, runtime.experiment_id)] = None
            self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            if self._stopping:
                return
            self._wake.clear()
            task_ready, self._task_ready = self._task_ready, {}
            for runner_id in task_ready:
                self._schedule_runner(runner_id)
            eval_ready, self._eval_ready = self._eval_ready, {}
            for runner_id, experiment_id in eval_ready:
                self._schedule_evals(runner_id, experiment_id)

    def _schedule_runner(self, runner_id: str) -> None:
        runner = self._runners.get(runner_id)
        if runner is None or not self._current(runner) or runner_id in self._dispatches:
            return
        cancel = runner.check_cancel and runner.cancel_retry is None
        runtime = None if cancel else runner.next_task()
        if not cancel and runtime is None:
            return
        item = runtime.reserve_task() if runtime is not None else None
        if runtime is not None:
            runner.update_candidate(runtime)
        self._dispatches[runner_id] = asyncio.create_task(
            self._run_dispatch(runner, runtime, item), name=f"dispatch-{runner_id}")

    async def _run_dispatch(self, runner: RunnerWork, runtime: ExperimentRuntime | None,
                            item: JobWorkItem | None) -> None:
        session = runner.session
        runner_id = session.identity.runner_id
        requeue = False
        try:
            async with self._dispatch_slots:
                if not self._current(runner):
                    return
                if runtime is None:
                    await self._run_cancel(runner)
                elif not runtime.cancelling:
                    assert item is not None
                    requeue = await self._dispatch_task(runner, runtime, item)
        except Exception as exc:
            logger.error("Dispatch failed runner_id=%r error_type=%s", runner_id, type(exc).__name__)
            session.request_close(1011)
        finally:
            if runtime is not None and item is not None:
                runtime.finish_dispatch(item, requeue=requeue)
                runner.update_candidate(runtime, first=requeue)
            runner.completed_during_dispatch.clear()
            self._dispatches.pop(runner_id, None)
            if (current := self._runners.get(runner_id)) is not None:
                self._signal_task(current)

    async def _dispatch_task(self, runner: RunnerWork, runtime: ExperimentRuntime, item: JobWorkItem) -> bool:
        session = runner.session
        claim = await self.dispatch.claim(session.identity, runtime, item, check_current=session.check_current)
        if isinstance(claim, ActiveAssignment):
            if claim.key.attempt_id not in runner.completed_during_dispatch:
                runner.active = claim.key
                if claim.cancel_reason is not None and claim.key.attempt_id != runner.cancel_ack:
                    runner.check_cancel = True
            return True  # 事务未领取，原 Job 仍可作为候选。
        if not isinstance(claim, DispatchClaim):
            self._apply_status(runner, runtime.experiment_id, claim.status)
            return claim.requeue
        if claim.key.attempt_id not in runner.completed_during_dispatch:
            runner.active = claim.key
        update = await send_dispatch(session, self.dispatch, claim, self.dispatch_timeout)
        if update.released and runner.active == claim.key:
            runner.active = None
        self._apply_status(runner, runtime.experiment_id, update.status)
        if update.requeue:
            self._retry(runner, "task_retry")
        return update.requeue

    async def _run_cancel(self, runner: RunnerWork) -> None:
        runner.check_cancel = False
        session = runner.session
        request = await self.dispatch.pending_cancel(session.identity)
        if (request is None or request.key.attempt_id == runner.cancel_ack
                or request.key.attempt_id in runner.completed_during_dispatch or not self._current(runner)):
            return
        if await send_cancel(session, request, self.dispatch_timeout, lambda: (
            self._current(runner) and request.key.attempt_id not in runner.completed_during_dispatch
        )):
            runner.cancel_ack = request.key.attempt_id
        elif self._current(runner):
            runner.check_cancel = True
            self._retry(runner, "cancel_retry")

    def _retry(self, runner: RunnerWork, timer: str) -> None:
        if self._current(runner):
            def wake_runner() -> None:
                setattr(runner, timer, None)
                self._signal_task(runner)

            setattr(runner, timer, asyncio.get_running_loop().call_later(self.retry_delay, wake_runner))

    def _schedule_evals(self, runner_id: str, experiment_id: int) -> None:
        runner = self._runners.get(runner_id)
        if runner is None or not self._current(runner):
            return
        runtime = runner.runtimes.get(experiment_id)
        if runtime is None or runtime.cancelling:
            return
        items, runtime.evals = runtime.evals, {}
        for key, item in items.items():
            if key not in self._eval_tasks:
                self._eval_tasks[key] = asyncio.create_task(
                    self._run_eval(runner_id, experiment_id, item), name=f"eval-{item.job_pk}")

    async def _run_eval(self, runner_id: str, experiment_id: int, item: EvaluationWorkItem) -> None:
        try:
            status = await self.evaluations.run(item)
            if (runner := self._runners.get(runner_id)) is not None:
                async with runner.loading:
                    if self._current(runner):
                        self._apply_status(runner, experiment_id, status)
        except Exception as exc:
            logger.error("Eval work failed job_pk=%s error_type=%s", item.job_pk, type(exc).__name__)
        finally:
            self._eval_tasks.pop(item.key, None)
