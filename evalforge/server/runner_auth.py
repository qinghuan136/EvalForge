"""Runner Bearer 凭证认证；浏览器 Cookie 和 user.auth_version 不作为 Runner 凭证。"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import load_only

from evalforge.db import Database
from evalforge.db.models import Runner, RunnerCredential, User

from .errors import APIError
from .security import token_digest

logger = logging.getLogger(__name__)
RUNNER_AUTH_CACHE_PREFIX = "evalforge:runner:authorization:"
RUNNER_AUTH_CACHE_TTL_SECONDS = 60


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RunnerIdentity:
    runner_id: str
    owner_user_id: int
    credential_id: int
    expires_at: datetime
    token_hash: bytes = field(repr=False)


class RunnerAuthorization(BaseModel):
    """仅缓存已通过校验的授权事实，Runner ID 由 Redis key 绑定。"""

    credential_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)
    expires_at: AwareDatetime
    token_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)

    @classmethod
    def from_identity(cls, identity: RunnerIdentity) -> "RunnerAuthorization":
        return cls(credential_id=identity.credential_id, owner_user_id=identity.owner_user_id,
                   expires_at=identity.expires_at, token_fingerprint=hashlib.sha256(identity.token_hash).hexdigest())

    def matches(self, identity: RunnerIdentity) -> bool:
        return (self.credential_id == identity.credential_id and self.owner_user_id == identity.owner_user_id
                and hmac.compare_digest(self.token_fingerprint, hashlib.sha256(identity.token_hash).hexdigest()))


class RunnerAuth:
    def __init__(self, database: Database, redis: Redis):
        self.database = database
        self.redis = redis

    @staticmethod
    def _identity(credential: RunnerCredential, runner: Runner, owner: User) -> RunnerIdentity:
        if credential.revoked_at is not None or utc(credential.expires_at) <= datetime.now(timezone.utc):
            raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
        if runner.deleted_at is not None or not runner.enabled or not owner.enabled:
            raise APIError(403, "runner_disabled", "Runner 或所属账号已停用")
        return RunnerIdentity(runner.runner_id, owner.id, credential.id, utc(credential.expires_at), credential.token_hash)

    async def authenticate(self, authorization: str | None) -> RunnerIdentity:
        scheme, _, token = (authorization or "").partition(" ")
        match = re.fullmatch(r"efr\.([A-Za-z0-9_-]{1,128})\.([A-Za-z0-9_-]{43})", token, re.ASCII)
        if scheme.lower() != "bearer" or match is None:
            raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
        lookup, secret = match.groups()
        digest = bytes.fromhex(token_digest(secret))
        checked_since = monotonic()
        async with self.database.transaction() as db:
            row = (await db.execute(select(RunnerCredential, Runner, User)
                                    .join(Runner, Runner.runner_id == RunnerCredential.runner_id)
                                    .join(User, User.id == Runner.owner_user_id)
                                    .where(RunnerCredential.token_id == lookup))).one_or_none()
            if row is None or not hmac.compare_digest(row[0].token_hash, digest):
                raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
            credential, runner, owner = row
            identity = self._identity(credential, runner, owner)
            credential.last_used_at = runner.last_seen_at = datetime.now(timezone.utc)
        await self._remember(identity, checked_since)
        return identity

    async def check(self, identity: RunnerIdentity) -> None:
        if identity.expires_at <= datetime.now(timezone.utc):
            raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
        raw = await self.redis.get(RUNNER_AUTH_CACHE_PREFIX + identity.runner_id)
        if raw is not None:
            try:
                authorization = RunnerAuthorization.model_validate_json(raw)
            except ValidationError:
                pass  # 损坏的快照不能授权，回源确认。
            else:
                if authorization.matches(identity):
                    if authorization.expires_at <= datetime.now(timezone.utc):
                        raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
                    return
        checked_since = monotonic()
        current = await self._check_database(identity)
        await self._remember(current, checked_since)

    async def _check_database(self, identity: RunnerIdentity) -> RunnerIdentity:
        async with self.database.transaction() as db:
            row = (await db.execute(select(RunnerCredential, Runner, User)
                                    .options(load_only(RunnerCredential.runner_id, RunnerCredential.revoked_at,
                                                       RunnerCredential.expires_at, RunnerCredential.token_hash),
                                             load_only(Runner.owner_user_id, Runner.enabled, Runner.deleted_at),
                                             load_only(User.enabled))
                                    .join(Runner, Runner.runner_id == RunnerCredential.runner_id)
                                    .join(User, User.id == Runner.owner_user_id)
                                    .where(RunnerCredential.id == identity.credential_id))).one_or_none()
            if row is None:
                raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
            current = self._identity(*row)
            if (current.runner_id != identity.runner_id or current.owner_user_id != identity.owner_user_id
                    or not hmac.compare_digest(current.token_hash, identity.token_hash)):
                raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
        return current

    async def _remember(self, identity: RunnerIdentity, checked_since: float) -> None:
        # 查询过程中若发生撤权，延迟回填也只在这次查询的有效窗口内可用。
        remaining = min(RUNNER_AUTH_CACHE_TTL_SECONDS - (monotonic() - checked_since),
                        (identity.expires_at - datetime.now(timezone.utc)).total_seconds())
        ttl_ms = int(remaining * 1000)
        if ttl_ms <= 0:
            return
        authorization = RunnerAuthorization.from_identity(identity)
        try:
            await self.redis.set(RUNNER_AUTH_CACHE_PREFIX + identity.runner_id,
                                 authorization.model_dump_json(), px=ttl_ms, nx=True)
        except RedisError as exc:
            # 刚刚通过数据库校验；缓存写入失败不改变认证结果。会话仍需 Redis 注册或续租。
            logger.debug("Runner authorization cache write unavailable error_type=%s", type(exc).__name__)
