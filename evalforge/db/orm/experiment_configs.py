"""可复用的实验配置，每行保存一个版本。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid, false, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import HasId, JSON_VALUE, UTC_TIMESTAMP


class ExperimentConfig(HasId):
    __tablename__ = "experiment_config"
    __table_args__ = (
        UniqueConstraint("config_group_id", "version"),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="valid_name"),
        CheckConstraint("length(trim(agent_target_id)) BETWEEN 1 AND 128", name="valid_agent_target"),
        CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 3600", name="valid_timeout"),
        CheckConstraint("repetitions BETWEEN 1 AND 100", name="valid_repetitions"),
    )

    # The writer generates one group ID for v1 and reuses it for subsequent versions.
    config_group_id: Mapped[UUID] = mapped_column(Uuid)
    version: Mapped[int]
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(2000))
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="NO ACTION"), index=True,
    )
    runner_id: Mapped[str] = mapped_column(ForeignKey("runners.runner_id", ondelete="RESTRICT"), index=True)
    agent_target_id: Mapped[str] = mapped_column(String(128))
    timeout_seconds: Mapped[float] = mapped_column(default=60.0, server_default="60")
    stream_output: Mapped[bool] = mapped_column(default=False, server_default=false())
    repetitions: Mapped[int] = mapped_column(default=1, server_default="1")
    evaluators: Mapped[list[dict[str, Any]]] = mapped_column(JSON_VALUE, default=list, server_default=text("'[]'"))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    user: Mapped["User"] = relationship()
    dataset: Mapped["Dataset"] = relationship()
    dataset_version: Mapped["DatasetVersion | None"] = relationship()
    runner: Mapped["Runner"] = relationship()
    runs: Mapped[list["Experiment"]] = relationship(back_populates="experiment_config", passive_deletes="all")
