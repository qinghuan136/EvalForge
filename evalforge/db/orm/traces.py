"""基础 OTLP Trace 和 Span；不解释或转换 AI 语义属性。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, ForeignKey, Index, Integer, Numeric,
    Text, UniqueConstraint, column, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UTC_TIMESTAMP

# SQLite 的自增主键需要 INTEGER；生产 PostgreSQL 仍使用 BIGINT。
_ROW_ID = BigInteger().with_variant(Integer(), "sqlite")
_UNIX_NANO = Numeric(20, 0)
_TIME_RANGE = (
    "start_time_unix_nano >= 0 AND end_time_unix_nano <= 18446744073709551615 "
    "AND end_time_unix_nano >= start_time_unix_nano"
)
_OTLP_JSON = JSON(none_as_null=True)


def _otel_id_check(name: str, digits: int) -> CheckConstraint:
    field = column(name, Text())
    return CheckConstraint(
        (func.length(field) == digits)
        & field.regexp_match(f"^[0-9a-f]{{{digits}}}$")
        & (field != "0" * digits),
        name=f"{name}_hex",
    )


class Trace(Base):
    """按用户归属聚合一组 Span；时间范围只反映已收到的 Span。"""

    __tablename__ = "traces"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "trace_id"),
        _otel_id_check("trace_id", 32),
        CheckConstraint(_TIME_RANGE, name="time_range"),
    )

    id: Mapped[int] = mapped_column(_ROW_ID, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    trace_id: Mapped[str] = mapped_column(Text)
    start_time_unix_nano: Mapped[Decimal] = mapped_column(_UNIX_NANO)
    end_time_unix_nano: Mapped[Decimal] = mapped_column(_UNIX_NANO)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=func.now())

    spans: Mapped[list["Span"]] = relationship(
        back_populates="trace", cascade="all, delete-orphan", passive_deletes=True,
    )


class Span(Base):
    """一个 OTLP Span；父 Span 可以晚到，也可以从未上报。"""

    __tablename__ = "spans"
    __table_args__ = (
        UniqueConstraint("trace_row_id", "span_id"),
        _otel_id_check("span_id", 16),
        _otel_id_check("parent_span_id", 16),
        CheckConstraint(_TIME_RANGE, name="time_range"),
        Index("ix_spans_trace_start_id", "trace_row_id", "start_time_unix_nano", "id"),
    )

    id: Mapped[int] = mapped_column(_ROW_ID, primary_key=True)
    trace_row_id: Mapped[int] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"))
    span_id: Mapped[str] = mapped_column(Text)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[int]
    start_time_unix_nano: Mapped[Decimal] = mapped_column(_UNIX_NANO)
    end_time_unix_nano: Mapped[Decimal] = mapped_column(_UNIX_NANO)
    status_code: Mapped[int | None]
    status_message: Mapped[str | None] = mapped_column(Text)
    trace_state: Mapped[str] = mapped_column(Text, server_default="")
    flags: Mapped[int] = mapped_column(BigInteger, server_default="0")
    attributes: Mapped[list[dict[str, Any]]] = mapped_column(_OTLP_JSON, server_default=text("'[]'"))
    events: Mapped[list[dict[str, Any]]] = mapped_column(_OTLP_JSON, server_default=text("'[]'"))
    links: Mapped[list[dict[str, Any]]] = mapped_column(_OTLP_JSON, server_default=text("'[]'"))
    resource: Mapped[dict[str, Any] | None] = mapped_column(_OTLP_JSON)
    instrumentation_scope: Mapped[dict[str, Any] | None] = mapped_column(_OTLP_JSON)
    dropped_attributes_count: Mapped[int] = mapped_column(BigInteger, server_default="0")
    dropped_events_count: Mapped[int] = mapped_column(BigInteger, server_default="0")
    dropped_links_count: Mapped[int] = mapped_column(BigInteger, server_default="0")

    trace: Mapped[Trace] = relationship(back_populates="spans")


Index("ix_traces_owner_start_id", Trace.owner_user_id, Trace.start_time_unix_nano.desc(), Trace.id.desc())
