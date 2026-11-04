"""读取 Runner 的 Redis 会话快照；实际 WSS 会话注册和连接关闭由后续会话层实现。"""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

from .errors import APIError
from .schemas.runners import RunnerConnection

RUNNER_SESSION_PREFIX = "evalforge:runner:session:"


class RegisteredConnection(BaseModel):
    session_id: str = Field(min_length=1)
    phase: Literal["connected", "syncing", "ready"]


class RunnerConnections:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def read(self, runner_ids: list[str]) -> list[RunnerConnection]:
        if not runner_ids:
            return []
        records = await self.redis.mget([RUNNER_SESSION_PREFIX + runner_id for runner_id in runner_ids])
        result = []
        for encoded in records:
            if encoded is None:
                result.append(RunnerConnection())
                continue
            try:
                record = RegisteredConnection.model_validate_json(encoded)
            except ValidationError:
                raise APIError(503, "runner_session_unavailable", "Runner 会话信息暂时不可用") from None
            result.append(RunnerConnection(online=True, phase=record.phase))
        return result

    async def invalidate(self, runner_id: str) -> None:
        # DB 已删除 / 撤销后清理注册；后续 WSS 层必须同时检查持久化身份状态。
        await self.redis.delete(RUNNER_SESSION_PREFIX + runner_id)
