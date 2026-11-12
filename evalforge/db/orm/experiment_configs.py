"""已停用的 Phoenix 风格后台配置；保留表及外键，不用于新远程执行链路。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint, func, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, HasId, JSON_VALUE, UTC_TIMESTAMP, deferred_reference


class ExperimentConfig(HasId):
    """已停用的实验执行配置，供现有数据库表与外键使用。"""

    __tablename__ = "experiment_jobs"
    __table_args__ = (
        CheckConstraint("type IN ('PROMPT', 'EVAL_ONLY')", name="valid_type"),
        CheckConstraint("status IN ('RUNNING', 'COMPLETED', 'STOPPED', 'ERROR')", name="valid_experiment_status"),
        UniqueConstraint("type", "id"),
        {"info": {"deprecated": True, "replacement": "experiments.execution_config"}},
    )
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_abstract": True}

    id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    type: Mapped[str]
    status: Mapped[str] = mapped_column(default="STOPPED", server_default="STOPPED")
    claimed_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    claimed_by: Mapped[str | None]
    cooldown_until: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    max_concurrency: Mapped[int] = mapped_column(default=10, server_default=text("10"))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    experiment: Mapped["Experiment"] = relationship(back_populates="legacy_config")
    dataset_evaluator_links: Mapped[list["ExperimentDatasetEvaluator"]] = relationship(back_populates="execution_config", cascade="all, delete-orphan", passive_deletes=True)


class ExperimentPromptTask(ExperimentConfig):
    __tablename__ = "experiment_prompt_tasks"
    __table_args__ = (
        CheckConstraint("type = 'PROMPT'", name="valid_type"),
        ForeignKeyConstraint(["type", "id"], ["experiment_jobs.type", "experiment_jobs.id"], ondelete="CASCADE"),
        CheckConstraint("NOT (custom_provider_id IS NOT NULL AND connection IS NOT NULL)", name="custom_provider_or_connection"),
    )
    __mapper_args__ = {"polymorphic_identity": "PROMPT"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(server_default="PROMPT")
    model_provider: Mapped[str]
    model_name: Mapped[str]
    custom_provider_id: Mapped[int | None] = deferred_reference("generative_model_custom_providers.id", index=True)
    template_type: Mapped[str]
    template_format: Mapped[str]
    template: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    tools: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON_VALUE)
    response_format: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    invocation_parameters: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, server_default=text("'{}'"))
    connection: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    prompt_version_id: Mapped[int | None] = deferred_reference("prompt_versions.id", index=True)
    playground_config: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    stream_model_output: Mapped[bool] = mapped_column(default=True, server_default=true())


class ExperimentEvalOnlyConfig(ExperimentConfig):
    # 单表继承：与 ExperimentConfig 共用 experiment_jobs，不创建第 18 张表。
    __mapper_args__ = {"polymorphic_identity": "EVAL_ONLY"}


class ExperimentDatasetEvaluator(Base):
    __tablename__ = "experiment_dataset_evaluators"

    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiment_jobs.id", ondelete="CASCADE"), primary_key=True)
    dataset_evaluator_id: Mapped[int] = mapped_column(ForeignKey("dataset_evaluators.id", ondelete="CASCADE"), primary_key=True, index=True)

    execution_config: Mapped["ExperimentConfig"] = relationship(back_populates="dataset_evaluator_links")
    dataset_evaluator: Mapped["DatasetEvaluators"] = relationship(back_populates="experiment_links")
