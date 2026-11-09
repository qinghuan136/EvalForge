"""云端派发尝试；不同于客户端 SQLite 的 RunnerAttempt。

本表保存派发归属和完整消息快照，不实现调度、自动重试或结果选择策略。
"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import HasId, JSON_VALUE, UTC_TIMESTAMP


class RunnerAttempt(HasId):
    __tablename__ = "runner_attempts"
    __table_args__ = (
        CheckConstraint("length(trim(attempt_id)) > 0 AND length(trim(runner_id)) > 0", name="identity"),
        CheckConstraint("status IN ('assigned','accepted','running','succeeded','failed','cancelled','unknown','rejected')", name="valid_status"),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') AND result_json IS NOT NULL AND result_received_at IS NOT NULL) OR "
            "(status IN ('assigned','accepted','running','unknown','rejected') AND result_json IS NULL AND result_received_at IS NULL)",
            name="result_state",
        ),
        CheckConstraint("status <> 'unknown' OR unknown_reason IS NOT NULL", name="unknown_reason"),
        CheckConstraint("status <> 'rejected' OR rejection_reason IS NOT NULL", name="rejection_reason"),
        CheckConstraint("cancel_reason IS NULL OR cancel_requested_at IS NOT NULL", name="cancel_intent"),
        # unknown 保留结果未知的历史，释放 Runner 和 Job 的活动名额。
        Index("uq_runner_attempts_active_runner", "runner_id", unique=True,
              postgresql_where=text("status IN ('assigned','accepted','running')"),
              sqlite_where=text("status IN ('assigned','accepted','running')")),
        Index("uq_runner_attempts_active_run", "experiment_run_id", unique=True,
              postgresql_where=text("status IN ('assigned','accepted','running')"),
              sqlite_where=text("status IN ('assigned','accepted','running')")),
    )

    attempt_id: Mapped[str] = mapped_column(unique=True, default=lambda: str(uuid4()))
    experiment_run_id: Mapped[int] = mapped_column(ForeignKey("experiment_runs.id", ondelete="CASCADE"), index=True)
    runner_id: Mapped[str] = mapped_column(ForeignKey("runners.runner_id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(default="assigned", server_default="assigned")
    dispatch_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    accepted_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    started_reported_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    result_received_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    cancel_reason: Mapped[str | None]
    unknown_reason: Mapped[str | None]
    rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    experiment_run: Mapped["ExperimentRun"] = relationship(back_populates="attempts")
    runner: Mapped["Runner"] = relationship(back_populates="attempts")
