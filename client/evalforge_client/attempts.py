"""Attempt 仓储：仅做持久化与条件更新，不执行 HTTP、RPC 或自动恢复。"""

import json
import math
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from .database import ClientDatabase
from .models import RunnerAttempt, TERMINAL_STATUSES, UNRESOLVED_STATUSES, encode_json, utc_now


class AttemptConflict(ValueError):
    """同一 Attempt 的派发参数或最终结果与已保存内容冲突。"""


class InvalidAttemptState(ValueError):
    """当前状态不允许请求的更新。"""


class AttemptRepository:
    def __init__(self, database: ClientDatabase) -> None:
        self.database = database

    @staticmethod
    def _key(job_id: str, attempt_id: str):
        return (RunnerAttempt.job_id == job_id, RunnerAttempt.attempt_id == attempt_id)

    @staticmethod
    async def _require(session, job_id, attempt_id) -> RunnerAttempt:
        row = await session.get(RunnerAttempt, (job_id, attempt_id))
        if row is None:
            raise KeyError((job_id, attempt_id))
        return row

    async def accept(
        self, *, job_id: str, attempt_id: str, agent_target_id: str, example_id: str,
        input_json: Any, timeout_seconds: float,
    ) -> RunnerAttempt:
        for value in (job_id, attempt_id, agent_target_id, example_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Attempt identifiers must be non-empty strings")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        payload = encode_json(input_json)
        async with self.database.session() as session:
            await session.execute(insert(RunnerAttempt).values(
                job_id=job_id, attempt_id=attempt_id, agent_target_id=agent_target_id,
                example_id=example_id, input_json=json.loads(payload), timeout_seconds=timeout_seconds,
            ).on_conflict_do_nothing(index_elements=["job_id", "attempt_id"]))
            row = await self._require(session, job_id, attempt_id)
            if (row.agent_target_id != agent_target_id or row.example_id != example_id
                    or row.timeout_seconds != timeout_seconds or encode_json(row.input_json) != payload):
                raise AttemptConflict("Attempt already exists with different dispatch parameters")
            return row

    async def get(self, job_id: str, attempt_id: str) -> RunnerAttempt | None:
        async with self.database.session() as session:
            return await session.get(RunnerAttempt, (job_id, attempt_id))

    async def claim(self, job_id: str, attempt_id: str) -> bool:
        """只有返回 True 的调用者取得执行权；返回发生在 running 已提交之后。"""
        async with self.database.session() as session:
            now = utc_now()
            result = await session.execute(update(RunnerAttempt).where(
                *self._key(job_id, attempt_id), RunnerAttempt.execution_status == "accepted",
                RunnerAttempt.cancel_requested == 0,
            ).values(execution_status="running", started_at=now, updated_at=now))
            return result.rowcount == 1

    async def request_cancel(self, job_id: str, attempt_id: str, reason: str | None = None) -> RunnerAttempt:
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Cancellation reason must be a string or None")
        async with self.database.session() as session:
            row = await self._require(session, job_id, attempt_id)
            if row.execution_status in TERMINAL_STATUSES or row.cancel_requested:
                return row
            row.cancel_requested, row.cancel_reason, row.updated_at = 1, reason, utc_now()
            return row

    async def finish(self, job_id: str, attempt_id: str, result_json: dict) -> RunnerAttempt:
        payload = json.loads(encode_json(result_json))
        if not isinstance(payload, dict) or payload.get("job_id") != job_id or payload.get("attempt_id") != attempt_id:
            raise ValueError("Result identity does not match the Attempt")
        status = payload.get("status")
        if status not in TERMINAL_STATUSES:
            raise ValueError("Result status must be succeeded, failed or cancelled")
        if not {"output", "duration_ms", "http_status", "error"} <= payload.keys():
            raise ValueError("Result must contain output, duration_ms, http_status and error")
        duration = payload["duration_ms"]
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            raise ValueError("duration_ms must be finite and non-negative")
        http_status = payload["http_status"]
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            raise ValueError("http_status must be an HTTP status code or None")
        error = payload["error"]
        if error is not None and (not isinstance(error, dict) or
                not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str)):
            raise ValueError("error must contain string code/message or be None")
        if status == "failed" and error is None:
            raise ValueError("Failed results must include an error")
        async with self.database.session() as session:
            row = await self._require(session, job_id, attempt_id)
            if row.execution_status in TERMINAL_STATUSES:
                if encode_json(row.result_json) != encode_json(payload):
                    raise AttemptConflict("Final result is immutable")
                return row
            if row.execution_status != "running" and not (row.execution_status == "accepted" and status == "cancelled"):
                raise InvalidAttemptState("Attempt cannot finish from its current state")
            now = utc_now()
            row.execution_status, row.result_json = status, payload
            row.delivery_status, row.finished_at, row.updated_at = "pending", now, now
            return row

    async def acknowledge(self, job_id: str, attempt_id: str) -> RunnerAttempt:
        async with self.database.session() as session:
            row = await self._require(session, job_id, attempt_id)
            if row.delivery_status == "acknowledged":
                return row
            if row.delivery_status != "pending":
                raise InvalidAttemptState("Attempt has no final result to acknowledge")
            now = utc_now()
            row.delivery_status, row.acknowledged_at, row.updated_at = "acknowledged", now, now
            return row

    async def recover_interrupted(self) -> int:
        """仅新进程启动时显式调用，不能用于重连；暂约定单进程，独占锁 TODO。"""
        async with self.database.session() as session:
            result = await session.execute(update(RunnerAttempt).where(
                RunnerAttempt.execution_status == "running",
            ).values(execution_status="unknown", unknown_reason="client_restarted", updated_at=utc_now()))
            return result.rowcount

    async def list_unresolved(self) -> list[RunnerAttempt]:
        return await self._list(RunnerAttempt.execution_status.in_(UNRESOLVED_STATUSES))

    async def list_pending_results(self) -> list[RunnerAttempt]:
        return await self._list(RunnerAttempt.delivery_status == "pending")

    async def sync_snapshot(self) -> tuple[list[RunnerAttempt], list[RunnerAttempt]]:
        """同一个短事务获取完整快照，避免两次查询之间任务完成导致遗漏。"""
        async with self.database.session() as session:
            rows = list(await session.scalars(select(RunnerAttempt).where(
                (RunnerAttempt.execution_status.in_(UNRESOLVED_STATUSES)) |
                (RunnerAttempt.delivery_status == "pending")
            ).order_by(RunnerAttempt.accepted_at, RunnerAttempt.job_id, RunnerAttempt.attempt_id)))
            return ([row for row in rows if row.execution_status in UNRESOLVED_STATUSES],
                    [row for row in rows if row.delivery_status == "pending"])

    async def _list(self, condition) -> list[RunnerAttempt]:
        async with self.database.session() as session:
            result = await session.scalars(select(RunnerAttempt).where(condition).order_by(
                RunnerAttempt.accepted_at, RunnerAttempt.job_id, RunnerAttempt.attempt_id,
            ))
            return list(result)
