"""实验、样本快照关联、TaskRun 与 EvalRun；结果集合不另建表。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, false, func, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, HasId, JSON_VALUE, UTC_TIMESTAMP, deferred_reference


class Experiment(HasId):
    __tablename__ = "experiments"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    name: Mapped[str]
    description: Mapped[str | None]
    repetitions: Mapped[int]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    is_ephemeral: Mapped[bool] = mapped_column(default=False, server_default=false())
    project_name: Mapped[str | None] = mapped_column(index=True)
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="experiments")
    dataset_version: Mapped["DatasetVersion"] = relationship(back_populates="experiments")
    experiment_dataset_examples: Mapped[list["ExperimentDatasetExample"]] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True)
    runs: Mapped[list["ExperimentRun"]] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True)
    job: Mapped["ExperimentJob | None"] = relationship(back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True, uselist=False)

    __table_args__ = (Index(
        "ix_experiments_ephemeral_updated_at", "updated_at",
        postgresql_where=text("is_ephemeral IS TRUE"), sqlite_where=text("is_ephemeral IS TRUE"),
    ),)


class ExperimentDatasetExample(Base):
    __tablename__ = "experiments_dataset_examples"

    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True)
    dataset_example_id: Mapped[int] = mapped_column(ForeignKey("dataset_examples.id", ondelete="CASCADE"), primary_key=True, index=True)
    dataset_example_revision_id: Mapped[int] = mapped_column(ForeignKey("dataset_example_revisions.id", ondelete="CASCADE"), index=True)

    experiment: Mapped["Experiment"] = relationship(back_populates="experiment_dataset_examples")
    dataset_example: Mapped["DatasetExample"] = relationship(back_populates="experiment_dataset_examples")
    dataset_example_revision: Mapped["DatasetExampleRevision"] = relationship(back_populates="experiment_dataset_examples")


class ExperimentRun(HasId):
    __tablename__ = "experiment_runs"
    __table_args__ = (UniqueConstraint("experiment_id", "dataset_example_id", "repetition_number"),)

    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"))
    dataset_example_id: Mapped[int] = mapped_column(ForeignKey("dataset_examples.id", ondelete="CASCADE"), index=True)
    repetition_number: Mapped[int]
    trace_id: Mapped[str | None]
    output: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    start_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    end_time: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    prompt_token_count: Mapped[int | None]
    completion_token_count: Mapped[int | None]
    error: Mapped[str | None]

    experiment: Mapped["Experiment"] = relationship(back_populates="runs")
    dataset_example: Mapped["DatasetExample"] = relationship(back_populates="runs")
    annotations: Mapped[list["ExperimentRunAnnotation"]] = relationship(back_populates="experiment_run", cascade="all, delete-orphan", passive_deletes=True)

    @hybrid_property
    def latency_ms(self) -> float:
        return (self.end_time - self.start_time).total_seconds() * 1000

    @latency_ms.inplace.expression
    @classmethod
    def _latency_expression(cls):
        return func.extract("epoch", cls.end_time - cls.start_time) * 1000


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
