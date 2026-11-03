"""密码使用 Argon2id；浏览器会话 Token 使用 32 字节随机数及 SHA-256 查询摘要。"""

import hashlib
import secrets

import anyio
from argon2 import PasswordHasher, Type
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError

from .errors import APIError


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Passwords:
    def __init__(self):
        # OWASP Argon2id 基线：19 MiB、两次迭代、一个 lane。
        self._hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1, type=Type.ID)
        self._limiter = anyio.CapacityLimiter(2)
        self._dummy_hash: str | None = None

    async def initialize(self) -> None:
        # 初始化发生在应用 lifespan；未知账号也执行一次真实摘要验证。
        self._dummy_hash = await self.hash(new_token())

    async def hash(self, password: str) -> str:
        try:
            return await anyio.to_thread.run_sync(self._hasher.hash, password, limiter=self._limiter)
        except HashingError:
            raise APIError(503, "password_unavailable", "密码服务暂时不可用") from None

    async def verify(self, encoded: str | None, password: str) -> bool:
        if self._dummy_hash is None:
            raise RuntimeError("Password service is not initialized")
        def check():
            try:
                matched = self._hasher.verify(encoded or self._dummy_hash, password)
                return matched and encoded is not None
            except (VerifyMismatchError, InvalidHashError):
                return False
            except VerificationError:
                raise APIError(503, "password_unavailable", "密码服务暂时不可用") from None
        return await anyio.to_thread.run_sync(check, limiter=self._limiter)

    def needs_rehash(self, encoded: str) -> bool:
        return self._hasher.check_needs_rehash(encoded)
