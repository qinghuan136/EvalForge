"""浏览器会话与 CSRF 存储；Token 只以摘要作为 Redis key，不自动续期。"""

import hmac
import re
from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from redis.asyncio import Redis

from .errors import APIError
from .security import new_token, token_digest

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
PREFIX = "evalforge:{auth}:session:"
_ROTATE = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if not redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2], 'NX') then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""
_RATE_LIMIT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class BrowserSession(BaseModel):
    csrf_token: str = Field(repr=False, pattern=r"^[A-Za-z0-9_-]{43}$")
    expires_at: AwareDatetime
    user_id: int | None = None
    auth_version: int | None = None


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis, self.ttl_seconds = redis, ttl_seconds

    async def load(self, token: str | None) -> BrowserSession | None:
        if token is None or not TOKEN_PATTERN.fullmatch(token):
            return None
        encoded = await self.redis.get(PREFIX + token_digest(token))
        if encoded is None:
            return None
        try:
            session = BrowserSession.model_validate_json(encoded)
        except ValidationError:
            raise APIError(503, "session_unavailable", "会话数据暂时不可用") from None
        return session if session.expires_at > datetime.now(timezone.utc) else None

    async def create_anonymous(self) -> tuple[str, BrowserSession]:
        token = new_token()
        record = BrowserSession(csrf_token=new_token(), expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
        if not await self.redis.set(PREFIX + token_digest(token), record.model_dump_json(), ex=600, nx=True):
            raise APIError(503, "session_unavailable", "无法建立会话")
        return token, record

    async def rotate(self, old_token: str, user_id: int, auth_version: int) -> tuple[str, BrowserSession]:
        token = new_token()
        record = BrowserSession(csrf_token=new_token(), user_id=user_id, auth_version=auth_version,
                                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds))
        # 原子消费旧会话，防止并发登录或退出后重新使用同一会话。
        changed = await self.redis.eval(_ROTATE, 2, PREFIX + token_digest(old_token), PREFIX + token_digest(token),
                                        record.model_dump_json(), self.ttl_seconds)
        if not changed:
            raise APIError(403, "csrf_invalid", "会话已失效，请重新获取 CSRF Token")
        return token, record

    async def delete(self, token: str) -> None:
        if TOKEN_PATTERN.fullmatch(token):
            await self.redis.delete(PREFIX + token_digest(token))

    async def limit(self, bucket: str, subject: str, maximum: int, seconds: int = 60) -> None:
        key = f"evalforge:auth:rate:{bucket}:{token_digest(subject)}"
        if await self.redis.eval(_RATE_LIMIT, 1, key, seconds) > maximum:
            raise APIError(429, "rate_limited", "请求过于频繁，请稍后再试")

    @staticmethod
    def verify_csrf(session: BrowserSession, provided: str | None) -> None:
        if (provided is None or not TOKEN_PATTERN.fullmatch(provided)
                or not hmac.compare_digest(session.csrf_token, provided)):
            raise APIError(403, "csrf_invalid", "CSRF 校验失败")
