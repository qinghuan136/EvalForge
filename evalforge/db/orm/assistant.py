"""Assistant storage models. Legacy tables remain until ConversationStore is connected."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, HasId, JSON_VALUE, UTC_TIMESTAMP


class AssistantConversationEntry(Base):
    __tablename__ = "assistant_conversation_entries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("assistant_conversation_entries.id", ondelete="RESTRICT"), index=True)
    message: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    kind: Mapped[str] = mapped_column(Text, default="message", server_default="message")

    __table_args__ = (
        CheckConstraint("length(trim(id)) > 0", name="id_nonblank"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        CheckConstraint("kind = 'message'", name="kind"),
    )


class AssistantConversationRecord(Base):
    __tablename__ = "assistant_conversation_records"

    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    head_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("assistant_conversation_entries.id", ondelete="RESTRICT"), index=True)
    revision: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, server_default=text("'{}'"))
    deferred_tool_requests: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    forked_from: Mapped[str | None] = mapped_column(Text)
    forked_at: Mapped[str | None] = mapped_column(
        Text, ForeignKey("assistant_conversation_entries.id", ondelete="RESTRICT"), index=True)
    branches: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list, server_default=text("'[]'"))
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    format_version: Mapped[int] = mapped_column(default=1, server_default="1")

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="新对话", server_default="新对话")
    status: Mapped[str] = mapped_column(String(24), default="completed", server_default="completed")
    active_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    resume_model_name: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(trim(conversation_id)) > 0", name="id_nonblank"),
        CheckConstraint("length(trim(revision)) > 0", name="revision_nonblank"),
        CheckConstraint("scope = 'user:' || CAST(user_id AS TEXT)", name="owner_scope"),
        CheckConstraint("length(trim(title)) > 0", name="title_nonblank"),
        CheckConstraint("format_version = 1", name="format_version"),
        CheckConstraint("status IN ('completed', 'running', 'awaiting_approval', 'failed')", name="status"),
        CheckConstraint("(active_run_id IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
        CheckConstraint("(status = 'running') = (active_run_id IS NOT NULL)", name="running_lease"),
        CheckConstraint("status <> 'awaiting_approval' OR deferred_tool_requests IS NOT NULL", name="approval_requests"),
        CheckConstraint("resume_model_name IS NULL OR length(trim(resume_model_name)) > 0", name="model_nonblank"),
    )


Index("ix_assistant_conversation_records_scope_updated_id", AssistantConversationRecord.scope,
      AssistantConversationRecord.updated_at.desc(), AssistantConversationRecord.conversation_id.desc())
Index("ix_assistant_conversation_records_user_updated_id", AssistantConversationRecord.user_id,
      AssistantConversationRecord.updated_at.desc(), AssistantConversationRecord.conversation_id.desc())


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
