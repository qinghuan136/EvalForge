"""评分器定义、源码版本，以及数据集上的输入输出映射。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import HasId, JSON_VALUE, UTC_TIMESTAMP, deferred_reference


class Evaluator(HasId):
    __tablename__ = "evaluators"
    __table_args__ = (
        CheckConstraint("kind IN ('LLM', 'CODE', 'BUILTIN')", name="valid_evaluator_kind"),
        UniqueConstraint("kind", "id"),
    )
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_abstract": True}

    kind: Mapped[str]
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    dataset_evaluators: Mapped[list["DatasetEvaluators"]] = relationship(back_populates="evaluator", cascade="all, delete-orphan", passive_deletes=True)


class LLMEvaluator(Evaluator):
    __tablename__ = "llm_evaluators"
    __table_args__ = (
        CheckConstraint("kind = 'LLM'", name="valid_type"),
        ForeignKeyConstraint(["kind", "id"], ["evaluators.kind", "evaluators.id"], ondelete="CASCADE"),
    )
    __mapper_args__ = {"polymorphic_identity": "LLM"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(server_default="LLM")
    prompt_id: Mapped[int] = deferred_reference("prompts.id", nullable=False, index=True, ondelete="RESTRICT")
    prompt_version_tag_id: Mapped[int | None] = deferred_reference("prompt_version_tags.id", index=True)
    output_configs: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE)
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())


class CodeEvaluator(Evaluator):
    __tablename__ = "code_evaluators"
    __table_args__ = (
        CheckConstraint("kind = 'CODE'", name="valid_type"),
        ForeignKeyConstraint(["kind", "id"], ["evaluators.kind", "evaluators.id"], ondelete="CASCADE"),
        {"info": {"deferred_foreign_key_constraints": [{
            "columns": ["sandbox_config_id", "language"],
            "targets": ["sandbox_configs.id", "sandbox_configs.language"],
        }]}},
    )
    __mapper_args__ = {"polymorphic_identity": "CODE"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(server_default="CODE")
    language: Mapped[str] = deferred_reference("languages.name", nullable=False, index=True, ondelete="RESTRICT", column_type=String)
    sandbox_config_id: Mapped[int | None] = deferred_reference("sandbox_configs.id", index=True)
    input_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, default=lambda: {"literal_mapping": {}, "path_mapping": {}},
        server_default=text("'{\"literal_mapping\": {}, \"path_mapping\": {}}'"),
    )
    output_configs: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE, default=list, server_default=text("'[]'"))
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    versions: Mapped[list["CodeEvaluatorVersion"]] = relationship(back_populates="code_evaluator", cascade="all, delete-orphan", passive_deletes=True)


class CodeEvaluatorVersion(HasId):
    __tablename__ = "code_evaluator_code_versions"
    __table_args__ = (
        Index("ix_code_evaluator_code_versions_code_evaluator_id_id", "code_evaluator_id", "id"),
        {"sqlite_autoincrement": True},
    )

    code_evaluator_id: Mapped[int] = mapped_column(ForeignKey("code_evaluators.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)
    source_code: Mapped[str] = mapped_column(server_default="")
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    code_evaluator: Mapped["CodeEvaluator"] = relationship(back_populates="versions")

    def has_identical_content(self, other: "CodeEvaluatorVersion") -> bool:
        return self.source_code == other.source_code


class BuiltinEvaluator(Evaluator):
    __tablename__ = "builtin_evaluators"
    __table_args__ = (
        CheckConstraint("kind = 'BUILTIN'", name="valid_type"),
        ForeignKeyConstraint(["kind", "id"], ["evaluators.kind", "evaluators.id"], ondelete="CASCADE"),
    )
    __mapper_args__ = {"polymorphic_identity": "BUILTIN"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(server_default="BUILTIN")
    key: Mapped[str] = mapped_column(unique=True)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    output_configs: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE)
    synced_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())


class DatasetEvaluators(HasId):
    __tablename__ = "dataset_evaluators"
    __table_args__ = (UniqueConstraint("dataset_id", "name"),)

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    evaluator_id: Mapped[int] = mapped_column(ForeignKey("evaluators.id", ondelete="CASCADE"), index=True)
    name: Mapped[str]
    description: Mapped[str | None]
    output_configs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_VALUE)
    input_mapping: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)
    project_id: Mapped[int] = deferred_reference("projects.id", nullable=False, index=True, ondelete="RESTRICT")
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="dataset_evaluators")
    evaluator: Mapped["Evaluator"] = relationship(back_populates="dataset_evaluators")
    experiment_links: Mapped[list["ExperimentDatasetEvaluator"]] = relationship(back_populates="dataset_evaluator", passive_deletes="all")
