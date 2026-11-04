"""Runner Bearer 凭证认证；浏览器 Cookie 和 user.auth_version 不作为 Runner 凭证。"""

import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from evalforge.db import Database
from evalforge.db.models import Runner, RunnerCredential, User

from .errors import APIError
from .security import token_digest


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RunnerIdentity:
    runner_id: str
    owner_user_id: int
    credential_id: int
    expires_at: datetime
    token_hash: bytes = field(repr=False)


class RunnerAuth:
    def __init__(self, database: Database):
        self.database = database

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
        return identity

    async def check(self, identity: RunnerIdentity) -> None:
        if identity.expires_at <= datetime.now(timezone.utc):
            raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
        async with self.database.transaction() as db:
            row = (await db.execute(select(RunnerCredential, Runner, User)
                                    .join(Runner, Runner.runner_id == RunnerCredential.runner_id)
                                    .join(User, User.id == Runner.owner_user_id)
                                    .where(RunnerCredential.id == identity.credential_id))).one_or_none()
            if row is None:
                raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
            current = self._identity(*row)
            if (current.runner_id != identity.runner_id or current.owner_user_id != identity.owner_user_id
                    or not hmac.compare_digest(current.token_hash, identity.token_hash)):
                raise APIError(401, "invalid_runner_credential", "Runner 凭证无效")
