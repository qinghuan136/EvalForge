"""为 Runner 领取 Job、读取取消意图并保存派发回执。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import Experiment, Job, RunnerAttempt

from ..experiment_runtime import ExperimentRuntime, JobWorkItem
from ..runner_auth import RunnerIdentity
from ..schemas.attempts import AttemptKey
from .attempts import ACTIVE, AttemptError, lock_owned_attempt, lock_runner
from .experiment_lifecycle import TERMINAL, finish_experiment, settle_experiment

RETRYABLE_REJECTIONS = {"busy", "not_ready", "session_unavailable"}
FAILURE_MESSAGES = {
    "target_not_found": "Runner 未配置实验指定的 Agent 目标",
    "executor_unavailable": "Runner 的执行层不可用",
    "attempt_conflict": "Runner 报告执行尝试参数冲突",
    "dispatch_rejected": "Runner 拒绝接单",
    "invalid_execution_config": "实验执行配置无效",
    "dispatch_payload_too_large": "派发消息超过 WebSocket 大小限制",
}


@dataclass(frozen=True)
class DispatchClaim:
    key: AttemptKey
    payload: dict
    experiment_id: int


@dataclass(frozen=True)
class CancelRequest:
    key: AttemptKey
    reason: str


@dataclass(frozen=True)
class ActiveAssignment:
    key: AttemptKey
    cancel_reason: str | None = None


@dataclass(frozen=True)
class DispatchUpdate:
    status: str | None = None
    requeue: bool = False
    changed: bool = False
    released: bool = False


async def active_assignment(db: AsyncSession, identity: RunnerIdentity) -> ActiveAssignment | None:
    row = (await db.execute(select(RunnerAttempt.attempt_id, RunnerAttempt.cancel_requested_at,
                                   RunnerAttempt.cancel_reason, Job.job_id)
           .join(Job, Job.id == RunnerAttempt.experiment_run_id)
           .join(Experiment, Experiment.id == Job.experiment_id)
           .where(RunnerAttempt.runner_id == identity.runner_id, RunnerAttempt.status.in_(ACTIVE),
                  Experiment.user_id == identity.owner_user_id))).one_or_none()
    if row is None:
        return None
    return ActiveAssignment(AttemptKey(job_id=row.job_id, attempt_id=row.attempt_id),
                            (row.cancel_reason or "user_requested") if row.cancel_requested_at else None)


def fail_job(job: Job, reason: str) -> None:
    job.status = "failed"
    job.output = {"task_output": None}
    job.error = {"code": reason, "message": FAILURE_MESSAGES[reason]}
    job.end_time = datetime.now(timezone.utc)


def cancel_unstarted_job(job: Job) -> None:
    job.status = "cancelled"
    job.output = {"task_output": None}
    job.end_time = datetime.now(timezone.utc)


class DispatchService:
    def __init__(self, database: Database):
        self.database = database

    async def pending_cancel(self, identity: RunnerIdentity) -> CancelRequest | None:
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
            except AttemptError:
                return None
            assignment = await active_assignment(db, identity)
            if assignment is not None and assignment.cancel_reason is not None:
                return CancelRequest(assignment.key, assignment.cancel_reason)
            return None

    async def claim(
        self, identity: RunnerIdentity, runtime: ExperimentRuntime, item: JobWorkItem, *,
        check_current: Callable[[], Awaitable[None]] | None = None,
    ) -> DispatchClaim | ActiveAssignment | DispatchUpdate:
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
            except AttemptError:
                return DispatchUpdate()
            # sync 锁同一 Runner。校验在锁内，避免旧会话在新会话对账之后创建预留。
            if check_current is not None:
                await check_current()
            occupied = await active_assignment(db, identity)
            if occupied is not None:
                return occupied
            row = (await db.execute(
                select(Job, Experiment).join(Experiment, Experiment.id == Job.experiment_id)
                .where(Job.id == item.job_pk, Job.job_id == item.job_id, Job.experiment_id == runtime.experiment_id,
                       Experiment.user_id == identity.owner_user_id,
                       Experiment.execution_config["runner_id"].as_string() == identity.runner_id)
                .with_for_update(of=(Job, Experiment))
            )).one_or_none()
            if row is None:
                return DispatchUpdate("cancelled")  # 已删除的实验不再调度。
            job, experiment = row
            if job.status != "queued" or experiment.status not in {"pending", "running"}:
                return DispatchUpdate(experiment.status)
            experiment.status = "running"
            config = runtime.execution_config
            if config is None:
                fail_job(job, "invalid_execution_config")
                await settle_experiment(db, experiment)
                return DispatchUpdate(experiment.status)
            key = AttemptKey(job_id=job.job_id, attempt_id=str(uuid4()))
            payload = {
                **key.model_dump(), "agent_target_id": config.agent_target_id,
                "example_id": str(item.example_id), "input": item.input,
                "timeout_seconds": config.timeout_seconds,
            }
            if config.stream_output:
                payload["stream_output"] = True
            job.status = "assigned"
            db.add(RunnerAttempt(attempt_id=key.attempt_id, experiment_run_id=job.id, runner_id=identity.runner_id,
                                 status="assigned", dispatch_json=payload))
            return DispatchClaim(key, payload, experiment.id)

    async def prepare_send(self, identity: RunnerIdentity, claim: DispatchClaim) -> DispatchUpdate | None:
        """在第一次发送 RPC 前确认预留仍可派发。"""
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
                attempt, job = await lock_owned_attempt(db, identity, claim.key)
            except AttemptError:
                return DispatchUpdate()
            if attempt.status != "assigned" or job.status != "assigned":
                return DispatchUpdate()
            if attempt.cancel_requested_at is None:
                return None
            # 当前派发协程尚未调用 RPC，可以确认这次预留未送达客户端。
            attempt.status, attempt.rejection_reason = "rejected", "cancelled_before_dispatch"
            cancel_unstarted_job(job)
            experiment = await finish_experiment(db, job.experiment_id, identity.runner_id)
            return DispatchUpdate(experiment.status if experiment else None, changed=True, released=True)

    async def record(self, identity: RunnerIdentity, claim: DispatchClaim, *,
                     outcome: Literal["accepted", "rejected", "unknown"], reason: str | None = None,
                     dispatched_at: datetime | None = None) -> DispatchUpdate:
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
                attempt, job = await lock_owned_attempt(db, identity, claim.key)
            except AttemptError:
                return DispatchUpdate()
            if dispatched_at is not None:
                attempt.dispatched_at = attempt.dispatched_at or dispatched_at
            if outcome == "accepted" and attempt.status in ({"assigned", "accepted", "running"} | TERMINAL):
                attempt.accepted_at = attempt.accepted_at or datetime.now(timezone.utc)
            # started、complete 和新会话的 sync 都可能先于派发回执提交。
            if attempt.status != "assigned" or job.status != "assigned":
                return DispatchUpdate()
            if outcome == "accepted":
                attempt.status = "accepted"
                return DispatchUpdate(changed=True)
            elif outcome == "unknown":
                attempt.status = job.status = "unknown"
                attempt.unknown_reason = reason or "dispatch_outcome_unknown"
            else:
                if reason not in RETRYABLE_REJECTIONS and reason not in FAILURE_MESSAGES:
                    reason = "dispatch_rejected"
                attempt.status = "rejected"
                attempt.unknown_reason = None
                attempt.rejection_reason = reason
                if attempt.cancel_requested_at is not None:
                    cancel_unstarted_job(job)
                elif reason in RETRYABLE_REJECTIONS:
                    job.status = "queued"
                    return DispatchUpdate(requeue=True, changed=True, released=True)
                else:
                    fail_job(job, reason)
            experiment = await finish_experiment(db, job.experiment_id, identity.runner_id)
            return DispatchUpdate(experiment.status if experiment else None, changed=True, released=True)
