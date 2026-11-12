"""Dataset：稳定样本身份 + 按版本记录的内容修订。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import JSON_VALUE, UTC_TIMESTAMP, HasId, deferred_reference


class Dataset(HasId):
    __tablename__ = "datasets"

    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)

    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True)
    examples: Mapped[list["DatasetExample"]] = relationship(back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True)
    experiments: Mapped[list["Experiment"]] = relationship(back_populates="dataset", passive_deletes="all")
    dataset_evaluators: Mapped[list["DatasetEvaluators"]] = relationship(back_populates="dataset", cascade="all, delete-orphan", passive_deletes=True)


class DatasetVersion(HasId):
    __tablename__ = "dataset_versions"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    description: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    user_id: Mapped[int | None] = deferred_reference("users.id", index=True)

    dataset: Mapped["Dataset"] = relationship(back_populates="versions")
    revisions: Mapped[list["DatasetExampleRevision"]] = relationship(back_populates="dataset_version", passive_deletes="all")
    experiments: Mapped[list["Experiment"]] = relationship(back_populates="dataset_version", passive_deletes="all")


class DatasetExample(HasId):
    __tablename__ = "dataset_examples"
    __table_args__ = (UniqueConstraint("dataset_id", "external_id"),)

    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    span_rowid: Mapped[int | None] = deferred_reference("spans.id", index=True)
    external_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    dataset: Mapped["Dataset"] = relationship(back_populates="examples")
    revisions: Mapped[list["DatasetExampleRevision"]] = relationship(back_populates="dataset_example", cascade="all, delete-orphan", passive_deletes=True)
    experiment_dataset_examples: Mapped[list["ExperimentDatasetExample"]] = relationship(back_populates="dataset_example", passive_deletes="all")
    jobs: Mapped[list["Job"]] = relationship(back_populates="dataset_example", passive_deletes="all")


class DatasetExampleRevision(HasId):
    __tablename__ = "dataset_example_revisions"
    __table_args__ = (
        UniqueConstraint("dataset_example_id", "dataset_version_id"),
        CheckConstraint("revision_kind IN ('CREATE', 'PATCH', 'DELETE')", name="valid_revision_kind"),
    )

    dataset_example_id: Mapped[int] = mapped_column(ForeignKey("dataset_examples.id", ondelete="CASCADE"))
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    output: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON_VALUE, default=dict)
    content_hash: Mapped[bytes | None] = mapped_column(LargeBinary, index=True)
    revision_kind: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    dataset_example: Mapped["DatasetExample"] = relationship(back_populates="revisions")
    dataset_version: Mapped["DatasetVersion"] = relationship(back_populates="revisions")
    experiment_dataset_examples: Mapped[list["ExperimentDatasetExample"]] = relationship(back_populates="dataset_example_revision", passive_deletes="all")
