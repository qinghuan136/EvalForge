"""实验、样本快照关联、TaskRun 与 EvalRun；结果集合不另建表。"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, false, func, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, HasId, JSON_VALUE, UTC_TIMESTAMP


class Experiment(HasId):
    __tablename__ = "experiments"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str]
    description: Mapped[str | None]
    repetitions: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending", server_default="pending")
    execution_config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, server_default=text("'{}'"))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    is_ephemeral: Mapped[bool] = mapped_column(default=False, server_default=false())
    project_name: Mapped[str | None] = mapped_column(index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="experiments")
    user: Mapped["User | None"] = relationship(back_populates="experiments")
    dataset_version: Mapped["DatasetVersion"] = relationship(back_populates="experiments")
    experiment_dataset_examples: Mapped[list["ExperimentDatasetExample"]] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True)
    runs: Mapped[list["ExperimentRun"]] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True)
    # 旧配置关联仅为保留现有外键；新执行链路使用 status / execution_config。
    job: Mapped["ExperimentJob | None"] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True, uselist=False)

    __table_args__ = (
        CheckConstraint("repetitions > 0", name="positive_repetitions"),
        CheckConstraint("status IN ('pending','running','cancelling','completed','failed','cancelled')", name="valid_status"),
        Index("ix_experiments_ephemeral_updated_at", "updated_at",
              postgresql_where=text("is_ephemeral IS TRUE"), sqlite_where=text("is_ephemeral IS TRUE")),
    )


class ExperimentDatasetExample(Base):
    __tablename__ = "experiments_dataset_examples"

    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True)
    dataset_example_id: Mapped[int] = mapped_column(ForeignKey("dataset_examples.id", ondelete="CASCADE"), primary_key=True, index=True)
    dataset_example_revision_id: Mapped[int] = mapped_column(ForeignKey("dataset_example_revisions.id", ondelete="CASCADE"), index=True)

    experiment: Mapped["Experiment"] = relationship(back_populates="experiment_dataset_examples")
    dataset_example: Mapped["DatasetExample"] = relationship(back_populates="experiment_dataset_examples")
    dataset_example_revision: Mapped["DatasetExampleRevision"] = relationship(back_populates="experiment_dataset_examples")


class ExperimentRun(HasId):
    """一条样本的一次逻辑 Job；Attempt 历史另表保存。"""
    __tablename__ = "experiment_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "dataset_example_id", "repetition_number"),
        CheckConstraint("length(trim(job_id)) > 0", name="job_identity"),
        CheckConstraint("repetition_number > 0", name="positive_repetition_number"),
        CheckConstraint("status IN ('queued','assigned','running','succeeded','failed','cancelled','unknown')", name="valid_status"),
        CheckConstraint("duration_ms IS NULL OR (duration_ms >= 0 AND duration_ms <= 1.7976931348623157e308)", name="duration"),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status"),
        CheckConstraint("end_time IS NULL OR start_time IS NULL OR end_time >= start_time", name="time_order"),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') AND output IS NOT NULL) OR "
            "(status IN ('queued','assigned','running','unknown') AND output IS NULL AND error IS NULL AND end_time IS NULL)",
            name="result_state",
        ),
        CheckConstraint("status <> 'failed' OR error IS NOT NULL", name="failed_error"),
        CheckConstraint("status <> 'succeeded' OR error IS NULL", name="succeeded_error"),
        Index("ix_experiment_runs_status_created_at", "status", "created_at"),
    )

    job_id: Mapped[str] = mapped_column(unique=True, default=lambda: str(uuid4()))
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"))
    dataset_example_id: Mapped[int] = mapped_column(ForeignKey("dataset_examples.id", ondelete="CASCADE"), index=True)
    repetition_number: Mapped[int]
    status: Mapped[str] = mapped_column(default="queued", server_default="queued")
    trace_id: Mapped[str | None]
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    start_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    end_time: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    duration_ms: Mapped[float | None]
    http_status: Mapped[int | None]
    prompt_token_count: Mapped[int | None]
    completion_token_count: Mapped[int | None]
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    experiment: Mapped["Experiment"] = relationship(back_populates="runs")
    dataset_example: Mapped["DatasetExample"] = relationship(back_populates="runs")
    annotations: Mapped[list["ExperimentRunAnnotation"]] = relationship(back_populates="experiment_run", cascade="all, delete-orphan", passive_deletes=True)
    attempts: Mapped[list["RunnerAttempt"]] = relationship(back_populates="experiment_run", cascade="all, delete-orphan", passive_deletes=True)

    @hybrid_property
    def latency_ms(self) -> float | None:
        if self.duration_ms is not None:
            return self.duration_ms
        if self.start_time is not None and self.end_time is not None:
            return (self.end_time - self.start_time).total_seconds() * 1000
        return None

    @latency_ms.inplace.expression
    @classmethod
    def _latency_expression(cls):
        return func.coalesce(cls.duration_ms, func.extract("epoch", cls.end_time - cls.start_time) * 1000)


class ExperimentRunAnnotation(HasId):
    __tablename__ = "experiment_run_annotations"
    __table_args__ = (
        UniqueConstraint("experiment_run_id", "name"),
        CheckConstraint("annotator_kind IN ('LLM', 'CODE', 'HUMAN')", name="valid_annotator_kind"),
    )

    experiment_run_id: Mapped[int] = mapped_column(ForeignKey("experiment_runs.id", ondelete="CASCADE"))
    name: Mapped[str]
    annotator_kind: Mapped[str]
    label: Mapped[str | None]
    score: Mapped[float | None]
    explanation: Mapped[str | None]
    trace_id: Mapped[str | None]
    error: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    end_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)

    experiment_run: Mapped["ExperimentRun"] = relationship(back_populates="annotations")
