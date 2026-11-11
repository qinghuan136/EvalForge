"""客户端 SQLite ORM，独立于云端模型。"""

import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, MetaData, REAL, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode_json(value: Any) -> str:
    """稳定编码；拒绝非 JSON 值、非字符串对象键和非有限数。"""
    def check(item: Any, ancestors: set[int]) -> None:
        if isinstance(item, (dict, list)):
            if id(item) in ancestors:
                raise ValueError("Circular JSON value")
            ancestors = ancestors | {id(item)}
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                children = item.values()
            else:
                children = item
            for child in children:
                check(child, ancestors)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Value is not JSON serializable")

    check(value, set())
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class JSONText(TypeDecorator):
    """磁盘存 TEXT，Python 端自动编解码；可空列允许 SQL NULL。"""

    impl = Text
    cache_ok = True

    def __init__(self, *, nullable: bool = False) -> None:
        super().__init__()
        self.nullable = nullable

    def process_bind_param(self, value, dialect):
        return None if value is None and self.nullable else encode_json(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class ExecutionStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    NOT_READY = "not_ready"
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"


TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
ACTIVE_STATUSES = ("accepted", "running")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "pk": "pk_%(table_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s",
    })


class RunnerAttempt(Base):
    __tablename__ = "runner_attempts"
    __table_args__ = (
        CheckConstraint("length(trim(job_id)) > 0 AND length(trim(attempt_id)) > 0", name="identity"),
        CheckConstraint("length(trim(agent_target_id)) > 0 AND length(trim(example_id)) > 0", name="target"),
        CheckConstraint("timeout_seconds > 0 AND timeout_seconds <= 1.7976931348623157e308", name="timeout"),
        CheckConstraint("json_valid(input_json)", name="input_json"),
        CheckConstraint("execution_status IN ('accepted','running','unknown','succeeded','failed','cancelled')", name="execution_status"),
        CheckConstraint("delivery_status IN ('not_ready','pending','acknowledged')", name="delivery_status"),
        CheckConstraint("cancel_requested IN (0, 1)", name="cancel_requested"),
        CheckConstraint(
            "(execution_status IN ('accepted','running','unknown') AND result_json IS NULL "
            "AND delivery_status = 'not_ready' AND finished_at IS NULL) OR "
            "(execution_status IN ('succeeded','failed','cancelled') AND result_json IS NOT NULL "
            "AND delivery_status IN ('pending','acknowledged') AND finished_at IS NOT NULL)", name="result_state",
        ),
        CheckConstraint(
            "(delivery_status = 'acknowledged' AND acknowledged_at IS NOT NULL) OR "
            "(delivery_status <> 'acknowledged' AND acknowledged_at IS NULL)", name="acknowledgment",
        ),
        CheckConstraint(
            "CASE WHEN result_json IS NULL THEN 1 WHEN NOT json_valid(result_json) THEN 0 ELSE "
            "json_type(result_json) = 'object' "
            "AND json_extract(result_json, '$.job_id') IS job_id "
            "AND json_extract(result_json, '$.attempt_id') IS attempt_id "
            "AND json_extract(result_json, '$.status') IS execution_status END", name="result_payload",
        ),
    )

    job_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    attempt_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    agent_target_id: Mapped[str] = mapped_column(Text)
    example_id: Mapped[str] = mapped_column(Text)
    input_json: Mapped[Any] = mapped_column(JSONText(), nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(REAL)
    stream_output: Mapped[bool] = mapped_column(Boolean(create_constraint=True, name="stream_output"),
                                               default=False, server_default="0")
    execution_status: Mapped[str] = mapped_column(Text, default="accepted", server_default="accepted")
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict | None] = mapped_column(JSONText(nullable=True))
    delivery_status: Mapped[str] = mapped_column(Text, default="not_ready", server_default="not_ready")
    unknown_reason: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[str] = mapped_column(Text, default=utc_now)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, default=utc_now)
