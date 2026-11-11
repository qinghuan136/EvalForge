"""Runner 有效会话注册、租约和条件清理；Socket 只保存在所属进程。"""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .errors import APIError
from .schemas.runners import RunnerConnection

RUNNER_SESSION_PREFIX = "evalforge:runner:session:"

# 比较与操作在 Redis 内原子完成；续租不能复活过期或被接管的注册。
_COMPARE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= 'table' or type(record.session_id) ~= 'string' then return -1 end
if record.session_id ~= ARGV[1] then return 0 end
if ARGV[2] == 'delete' then return redis.call('DEL', KEYS[1]) end
if redis.call('PTTL', KEYS[1]) <= 0 then return 0 end
if ARGV[2] == 'renew' then return redis.call('EXPIRE', KEYS[1], ARGV[3]) end
if ARGV[2] == 'phase' then
    if record.phase ~= ARGV[3] then return 0 end
    if redis.call('SET', KEYS[1], ARGV[4], 'XX', 'KEEPTTL') then return 1 end
    return 0
end
return 1
"""


class RegisteredConnection(BaseModel):
    session_id: str = Field(min_length=1)
    phase: Literal["connected", "syncing", "ready"]
    capabilities: list[str] = Field(default_factory=list, max_length=32)


class RunnerConnections:
    def __init__(self, redis: Redis):
        self.redis = redis

    @staticmethod
    def _decode(encoded: str | bytes) -> RegisteredConnection:
        try:
            return RegisteredConnection.model_validate_json(encoded)
        except ValidationError:
            raise APIError(503, "runner_session_unavailable", "Runner 会话信息暂时不可用") from None

    async def read_current(self, runner_id: str) -> RegisteredConnection | None:
        """在同一次 Redis 事务中读取注册和剩余租期，只有有效租约算在线。"""
        key = RUNNER_SESSION_PREFIX + runner_id
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                encoded, ttl_ms = await pipe.get(key).pttl(key).execute()
        except RedisError:
            raise APIError(503, "runner_session_unavailable", "Runner 会话信息暂时不可用") from None
        return None if encoded is None or ttl_ms <= 0 else self._decode(encoded)

    async def read(self, runner_ids: list[str]) -> list[RunnerConnection]:
        if not runner_ids:
            return []
        records = await self.redis.mget([RUNNER_SESSION_PREFIX + runner_id for runner_id in runner_ids])
        result = []
        for encoded in records:
            if encoded is None:
                result.append(RunnerConnection())
                continue
            record = self._decode(encoded)
            result.append(RunnerConnection(online=True, phase=record.phase))
        return result

    async def invalidate(self, runner_id: str) -> None:
        # DB 已删除 / 撤销后清理注册；后续 WSS 层必须同时检查持久化身份状态。
        await self.redis.delete(RUNNER_SESSION_PREFIX + runner_id)

    async def register(self, runner_id: str, record: RegisteredConnection, ttl_seconds: int) -> str | None:
        """原子替换并返回被接管的 session_id，便于立即通知本进程旧连接。"""
        previous = await self.redis.set(RUNNER_SESSION_PREFIX + runner_id, record.model_dump_json(),
                                        ex=ttl_seconds, get=True)
        if previous is None:
            return None
        try:
            return RegisteredConnection.model_validate_json(previous).session_id
        except ValidationError:
            # 新注册已经写入；损坏的旧快照不阻止合法连接重新建立。
            return None

    async def _compare(self, runner_id: str, session_id: str, operation: str, *args: str | int) -> bool:
        result = await self.redis.eval(_COMPARE, 1, RUNNER_SESSION_PREFIX + runner_id,
                                       session_id, operation, *args)
        if result == -1:
            raise APIError(503, "runner_session_unavailable", "Runner 会话信息暂时不可用")
        return bool(result)

    async def is_current(self, runner_id: str, session_id: str) -> bool:
        return await self._compare(runner_id, session_id, "check")

    async def renew(self, runner_id: str, session_id: str, ttl_seconds: int) -> bool:
        return await self._compare(runner_id, session_id, "renew", ttl_seconds)

    async def compare_delete(self, runner_id: str, session_id: str) -> bool:
        return await self._compare(runner_id, session_id, "delete")

    async def compare_and_set_phase(self, runner_id: str, session_id: str, expected_phase: str,
                                    record: RegisteredConnection) -> bool:
        if record.session_id != session_id:
            raise ValueError("Registration identity mismatch")
        # 由 Python 序列化完整快照，避免 Lua cjson 重编码把空 capabilities 数组变成对象。
        return await self._compare(runner_id, session_id, "phase", expected_phase, record.model_dump_json())
