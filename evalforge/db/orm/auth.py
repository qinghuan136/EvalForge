"""用户、Runner 身份及凭证记录；签发、验证和会话管理由后续服务实现。"""

from calendar import monthrange
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, HasId, UTC_TIMESTAMP


def _credential_expiration(context) -> datetime:
    """ORM 插入默认有效期为签发后十个日历年；闰日落到目标年二月末。"""
    created_at = context.get_current_parameters()["created_at"]
    year = created_at.year + 10
    return created_at.replace(year=year, day=min(created_at.day, monthrange(year, created_at.month)[1]))


class User(HasId):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(trim(username)) > 0", name="username"),
        CheckConstraint("length(trim(password_hash)) > 0", name="password_hash"),
        CheckConstraint("auth_version > 0", name="auth_version"),
    )

    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    display_name: Mapped[str | None]
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    auth_version: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)

    runners: Mapped[list["Runner"]] = relationship(back_populates="owner", passive_deletes="all")
    experiments: Mapped[list["Experiment"]] = relationship(back_populates="user", passive_deletes="all")


class Runner(Base):
    __tablename__ = "runners"
    __table_args__ = (
        CheckConstraint("length(trim(runner_id)) > 0 AND length(trim(name)) > 0", name="identity"),
    )

    runner_id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now(), onupdate=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)

    owner: Mapped[User] = relationship(back_populates="runners")
    credentials: Mapped[list["RunnerCredential"]] = relationship(back_populates="runner", passive_deletes="all")
    attempts: Mapped[list["RunnerAttempt"]] = relationship(back_populates="runner", passive_deletes="all")


class RunnerCredential(HasId):
    __tablename__ = "runner_credentials"
    __table_args__ = (
        CheckConstraint("length(trim(token_id)) > 0", name="token_id"),
        CheckConstraint("length(token_hash) = 32", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="expiration"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="revocation"),
    )

    runner_id: Mapped[str] = mapped_column(ForeignKey("runners.runner_id", ondelete="RESTRICT"), index=True)
    token_id: Mapped[str] = mapped_column(unique=True, default=lambda: uuid4().hex)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    name: Mapped[str | None]
    # Python 默认值保证离线测试与 PostgreSQL 行为相同；原生 SQL 必须显式提供 expires_at。
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, default=_credential_expiration)
    revoked_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)
    last_used_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP)

    runner: Mapped[Runner] = relationship(back_populates="credentials")
