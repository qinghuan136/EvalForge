"""执行尝试的归属、事务和幂等规则；不调度、不重试，也不记录入口日志。"""

from datetime import datetime, timezone
import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import Experiment, ExperimentRun, Runner, RunnerAttempt

from ..runner_auth import RunnerIdentity
from ..schemas.attempts import AttemptKey, AttemptResult, RunnerSync

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"assigned", "accepted", "running", "unknown"}


class AttemptError(ValueError):
    """可预期的业务拒绝；message 只能使用服务端固定文本。"""


async def lock_runner(db: AsyncSession, identity: RunnerIdentity) -> None:
    # 同一 Runner 的短事务串行化；不持锁等待网络或 Agent。
    runner = await db.scalar(select(Runner.runner_id).where(
        Runner.runner_id == identity.runner_id, Runner.owner_user_id == identity.owner_user_id,
        Runner.deleted_at.is_(None), Runner.enabled.is_(True),
    ).with_for_update())
    if runner is None:
        raise AttemptError("Runner unavailable")


async def lock_owned_attempt(db: AsyncSession, identity: RunnerIdentity,
                             key: AttemptKey) -> tuple[RunnerAttempt, ExperimentRun]:
    job = await db.scalar(select(ExperimentRun).join(Experiment).where(
        ExperimentRun.job_id == key.job_id, Experiment.user_id == identity.owner_user_id,
    ).with_for_update(of=ExperimentRun))
    if job is None:
        raise AttemptError("Attempt not found or not assigned to this Runner")
    attempt = await db.scalar(select(RunnerAttempt).where(
        RunnerAttempt.attempt_id == key.attempt_id, RunnerAttempt.experiment_run_id == job.id,
        RunnerAttempt.runner_id == identity.runner_id,
    ).with_for_update())
    if attempt is None:
        raise AttemptError("Attempt not found or not assigned to this Runner")
    return attempt, job


async def finish_experiment(db: AsyncSession, job: ExperimentRun, runner_id: str) -> None:
    # V1 一个实验绑定一个 Runner，最后一个终态写入和收尾受同一 Runner 行锁保护。
    incomplete = select(ExperimentRun.id).where(
        ExperimentRun.experiment_id == Experiment.id, ExperimentRun.status.not_in(TERMINAL),
    ).exists()
    await db.execute(update(Experiment).where(
        Experiment.id == job.experiment_id, Experiment.status == "running",
        Experiment.execution_config["runner_id"].as_string() == runner_id,
        ~incomplete,
    ).values(status="completed").execution_options(synchronize_session=False))


class AttemptService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _same_result(attempt: RunnerAttempt, result: AttemptResult) -> None:
        # Python 的 True == 1，但它们是不同 JSON 结果；键顺序不影响幂等。
        def encoded(value):
            return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if encoded(attempt.result_json) != encoded(result.model_dump()):
            raise AttemptError("Conflicting final result")

    @staticmethod
    def _can_update(attempt: RunnerAttempt, job: ExperimentRun) -> None:
        if attempt.status not in ACTIVE or job.status in TERMINAL:
            raise AttemptError("Attempt cannot update this Job")

    async def started(self, identity: RunnerIdentity, key: AttemptKey) -> None:
        async with self.database.transaction() as db:
            await lock_runner(db, identity)
            attempt, job = await lock_owned_attempt(db, identity, key)
            if attempt.status in TERMINAL:
                return  # 迟到的 started 不覆盖终态。
            self._can_update(attempt, job)
            now = datetime.now(timezone.utc)
            attempt.started_reported_at = attempt.started_reported_at or now
            job.start_time = job.start_time or now
            # 一条迟到的 started 不能证明 unknown 的执行仍然存活。
            if attempt.status != "unknown":
                attempt.status = job.status = "running"

    async def complete(self, identity: RunnerIdentity, result: AttemptResult) -> None:
        async with self.database.transaction() as db:
            await lock_runner(db, identity)
            attempt, job = await lock_owned_attempt(db, identity, result)
            if attempt.status in TERMINAL:
                self._same_result(attempt, result)
                return
            self._can_update(attempt, job)
            now = datetime.now(timezone.utc)
            attempt.status = job.status = result.status
            attempt.result_json, attempt.result_received_at = result.model_dump(), now
            attempt.unknown_reason = None
            job.output = {"task_output": result.output}
            job.error = result.error.model_dump() if result.error is not None else None
            job.duration_ms, job.http_status, job.end_time = result.duration_ms, result.http_status, now
            await finish_experiment(db, job, identity.runner_id)
        # 离开事务（包括 commit）成功后，调用方才可返回 ACK。

    async def sync(self, identity: RunnerIdentity, snapshot: RunnerSync) -> dict:
        if snapshot.runner_id != identity.runner_id:
            raise AttemptError("Runner identity mismatch")
        instructions = []
        seen = {(row.job_id, row.attempt_id) for row in [*snapshot.active_jobs, *snapshot.pending_results]}
        async with self.database.transaction() as db:
            await lock_runner(db, identity)
            for result in snapshot.pending_results:
                attempt, job = await lock_owned_attempt(db, identity, result)
                if attempt.status in TERMINAL:
                    self._same_result(attempt, result)
                else:
                    self._can_update(attempt, job)
                instructions.append({"job_id": result.job_id, "attempt_id": result.attempt_id,
                                     "action": "retry_upload"})
            for local in snapshot.active_jobs:
                attempt, job = await lock_owned_attempt(db, identity, local)
                cancel = attempt.status in TERMINAL or attempt.status == "rejected"
                if not cancel:
                    self._can_update(attempt, job)
                    now = datetime.now(timezone.utc)
                    experiment_status = await db.scalar(select(Experiment.status).where(Experiment.id == job.experiment_id))
                    cancel = (attempt.cancel_requested_at is not None
                              or experiment_status in {"cancelling", "cancelled"})
                    if cancel:
                        attempt.cancel_requested_at = attempt.cancel_requested_at or now
                        attempt.cancel_reason = attempt.cancel_reason or "experiment_cancelled"
                    if local.status == "unknown":
                        attempt.status = job.status = "unknown"
                        attempt.unknown_reason = "client_execution_unknown"
                        attempt.cancel_requested_at = attempt.cancel_requested_at or now
                        attempt.cancel_reason = attempt.cancel_reason or "client_execution_unknown"
                        cancel = True
                    elif local.status == "running":
                        attempt.status = job.status = "running"
                        attempt.unknown_reason = None
                        attempt.started_reported_at = attempt.started_reported_at or now
                        job.start_time = job.start_time or now
                    elif attempt.status in {"assigned", "accepted"}:
                        attempt.status = "accepted"
                        attempt.accepted_at = attempt.accepted_at or now
                    else:
                        # 本地 accepted 与云端已开始/unknown 矛盾，不能指令重新执行。
                        attempt.status = job.status = "unknown"
                        attempt.unknown_reason = "sync_state_conflict"
                        attempt.cancel_requested_at = attempt.cancel_requested_at or now
                        attempt.cancel_reason = attempt.cancel_reason or "sync_state_conflict"
                        cancel = True
                instructions.append({"job_id": local.job_id, "attempt_id": local.attempt_id,
                                     "action": "cancel" if cancel else "continue"})
            # 反向对账：客户端没有报告的活跃尝试不能直接释放或重新派发。
            rows = (await db.execute(select(RunnerAttempt, ExperimentRun)
                                     .join(ExperimentRun).join(Experiment)
                                     .where(RunnerAttempt.runner_id == identity.runner_id,
                                            RunnerAttempt.status.in_(ACTIVE),
                                            Experiment.user_id == identity.owner_user_id))).all()
            for attempt, job in rows:
                if (job.job_id, attempt.attempt_id) not in seen:
                    attempt.status = job.status = "unknown"
                    attempt.unknown_reason = "missing_from_client_snapshot"
        return {"jobs": instructions}
