"""为 Runner 领取 Job、读取取消意图并保存派发回执。"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from evalforge.db import Database
from evalforge.db.models import (
    DatasetExampleRevision, Experiment, ExperimentDatasetExample, ExperimentRun, RunnerAttempt,
)

from ..runner_auth import RunnerIdentity
from ..schemas.attempts import AttemptKey
from ..schemas.experiments import ExecutionConfig
from .attempts import ACTIVE, TERMINAL, AttemptError, finish_experiment, lock_owned_attempt, lock_runner

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


@dataclass(frozen=True)
class CancelRequest:
    key: AttemptKey
    reason: str


def fail_job(job: ExperimentRun, reason: str) -> None:
    job.status = "failed"
    job.output = {"task_output": None}
    job.error = {"code": reason, "message": FAILURE_MESSAGES[reason]}
    job.end_time = datetime.now(timezone.utc)


def cancel_unstarted_job(job: ExperimentRun) -> None:
    job.status = "cancelled"
    job.output = {"task_output": None}
    job.end_time = datetime.now(timezone.utc)


class DispatchService:
    def __init__(self, database: Database):
        self.database = database

    async def next_action(self, identity: RunnerIdentity) -> DispatchClaim | CancelRequest | None:
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
            except AttemptError:
                return None
            occupied = (await db.execute(select(
                RunnerAttempt.attempt_id, RunnerAttempt.cancel_requested_at, RunnerAttempt.cancel_reason,
                ExperimentRun.job_id, Experiment.user_id,
            ).join(ExperimentRun, ExperimentRun.id == RunnerAttempt.experiment_run_id)
             .join(Experiment, Experiment.id == ExperimentRun.experiment_id).where(
                RunnerAttempt.runner_id == identity.runner_id, RunnerAttempt.status.in_(ACTIVE),
            ).limit(1))).one_or_none()
            if occupied is not None:
                if occupied.cancel_requested_at is not None and occupied.user_id == identity.owner_user_id:
                    return CancelRequest(AttemptKey(job_id=occupied.job_id, attempt_id=occupied.attempt_id),
                                         occupied.cancel_reason or "user_requested")
                return None

            job, snapshot, revision = ExperimentRun, ExperimentDatasetExample, DatasetExampleRevision
            row = (await db.execute(
                select(job, Experiment, revision.input)
                .join(Experiment, Experiment.id == job.experiment_id)
                .join(snapshot, (snapshot.experiment_id == job.experiment_id)
                                & (snapshot.dataset_example_id == job.dataset_example_id))
                .join(revision, revision.id == snapshot.dataset_example_revision_id)
                .where(job.status == "queued", Experiment.status.in_(("pending", "running")),
                       Experiment.user_id == identity.owner_user_id,
                       Experiment.execution_config["runner_id"].as_string() == identity.runner_id)
                .order_by(job.created_at, job.id).limit(1)
                .with_for_update(of=(job, Experiment), skip_locked=True)
            )).one_or_none()
            if row is None:
                return None

            run, experiment, example_input = row
            experiment.status = "running"
            try:
                config = ExecutionConfig.model_validate(experiment.execution_config)
            except ValidationError:
                fail_job(run, "invalid_execution_config")
                await finish_experiment(db, run, identity.runner_id)
                return None
            key = AttemptKey(job_id=run.job_id, attempt_id=str(uuid4()))
            payload = {
                **key.model_dump(), "agent_target_id": config.agent_target_id,
                "example_id": str(run.dataset_example_id), "input": example_input,
                "timeout_seconds": config.timeout_seconds,
            }
            run.status = "assigned"
            db.add(RunnerAttempt(attempt_id=key.attempt_id, experiment_run_id=run.id, runner_id=identity.runner_id,
                                 status="assigned", dispatch_json=payload))
            return DispatchClaim(key, payload)

    async def prepare_send(self, identity: RunnerIdentity, claim: DispatchClaim) -> bool:
        """在第一次发送 RPC 前确认预留仍可派发。"""
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
                attempt, job = await lock_owned_attempt(db, identity, claim.key)
            except AttemptError:
                return False
            if attempt.status != "assigned" or job.status != "assigned":
                return False
            if attempt.cancel_requested_at is None:
                return True
            # 当前派发协程尚未调用 RPC，可以确认这次预留未送达客户端。
            attempt.status, attempt.rejection_reason = "rejected", "cancelled_before_dispatch"
            cancel_unstarted_job(job)
            await finish_experiment(db, job, identity.runner_id)
            return False

    async def record(self, identity: RunnerIdentity, claim: DispatchClaim, *,
                     outcome: Literal["accepted", "rejected", "unknown"], reason: str | None = None,
                     dispatched_at: datetime | None = None) -> bool:
        async with self.database.transaction() as db:
            try:
                await lock_runner(db, identity)
                attempt, job = await lock_owned_attempt(db, identity, claim.key)
            except AttemptError:
                return False
            if dispatched_at is not None:
                attempt.dispatched_at = attempt.dispatched_at or dispatched_at
            if outcome == "accepted" and attempt.status in ({"assigned", "accepted", "running"} | TERMINAL):
                attempt.accepted_at = attempt.accepted_at or datetime.now(timezone.utc)
            # started、complete 和新会话的 sync 都可能先于派发回执提交。
            if attempt.status != "assigned" or job.status != "assigned":
                return False
            if outcome == "accepted":
                attempt.status = "accepted"
            elif outcome == "unknown":
                attempt.status = job.status = "unknown"
                attempt.unknown_reason = reason or "dispatch_outcome_unknown"
                await finish_experiment(db, job, identity.runner_id)
            else:
                if reason not in RETRYABLE_REJECTIONS and reason not in FAILURE_MESSAGES:
                    reason = "dispatch_rejected"
                attempt.status = "rejected"
                attempt.unknown_reason = None
                attempt.rejection_reason = reason
                if attempt.cancel_requested_at is not None:
                    cancel_unstarted_job(job)
                    await finish_experiment(db, job, identity.runner_id)
                elif reason in RETRYABLE_REJECTIONS:
                    job.status = "queued"
                else:
                    fail_job(job, reason)
                    await finish_experiment(db, job, identity.runner_id)
            return True
