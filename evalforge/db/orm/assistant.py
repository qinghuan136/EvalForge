"""内置助手会话、执行租约和消息。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import HasId, JSON_VALUE, UTC_TIMESTAMP


class AssistantSession(HasId):
    __tablename__ = "assistant_sessions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), server_default="新对话")
    version: Mapped[int] = mapped_column(server_default="0")
    active_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    __table_args__ = (
        CheckConstraint("version >= 0", name="version"),
        CheckConstraint("(active_run_id IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
    )


class AssistantMessage(HasId):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="role"),
        CheckConstraint("status IN ('completed', 'interrupted', 'failed')", name="status"),
        CheckConstraint(
            "(role = 'assistant' AND model_provider IS NOT NULL AND model_name IS NOT NULL) OR "
            "(role <> 'assistant' AND model_provider IS NULL AND model_name IS NULL)",
            name="role_model",
        ),
        Index("ix_assistant_messages_session_id_id", "session_id", "id"),
    )

    session_id: Mapped[int] = mapped_column(ForeignKey("assistant_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    status: Mapped[str] = mapped_column(String(16), server_default="completed")
    model_provider: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())


Index("ix_assistant_sessions_user_updated_id", AssistantSession.user_id,
      AssistantSession.updated_at.desc(), AssistantSession.id.desc())
