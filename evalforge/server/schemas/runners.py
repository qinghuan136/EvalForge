"""Runner 管理的输入与响应白名单；完整 Token 仅出现在创建响应中。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator

from .auth import Schema


class RunnerName(Schema):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class RunnerConnection(Schema):
    online: bool = False
    phase: Literal["disconnected", "connected", "syncing", "ready"] = "disconnected"


class RunnerCreated(Schema):
    runner_id: str
    name: str
    enabled: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value):
        # TIMESTAMPTZ 为 UTC 时刻；离线 SQLite 变体会丢失 tzinfo。
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class RunnerResponse(RunnerCreated):
    updated_at: datetime
    last_seen_at: datetime | None
    connection: RunnerConnection = Field(default_factory=RunnerConnection)

    @field_validator("updated_at", "last_seen_at")
    @classmethod
    def utc_observed_at(cls, value):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CredentialIssued(Schema):
    id: int
    token_id: str
    token: str = Field(repr=False)
    expires_at: datetime


class RunnerEndpoint(Schema):
    websocket_url: str


class RunnerCreateResponse(Schema):
    runner: RunnerCreated
    credential: CredentialIssued
    connection: RunnerEndpoint


class RunnerPage(Schema):
    items: list[RunnerResponse]
    page: int
    pagesize: int
    total: int
