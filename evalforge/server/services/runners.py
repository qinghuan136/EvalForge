"""Runner 管理：归属检查、唯一凭证签发、分页查询和可重试的逻辑删除。"""

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import Runner, RunnerCredential

from ..errors import APIError
from ..runner_connections import RunnerConnections
from ..schemas.runners import (
    CredentialIssued, RunnerCreated, RunnerCreateResponse, RunnerEndpoint, RunnerPage, RunnerResponse,
)
from ..security import new_token, token_digest
from .auth import locked_user


class RunnerService:
    def __init__(self, database: Database, connections: RunnerConnections, websocket_url: str):
        self.database, self.connections, self.websocket_url = database, connections, websocket_url

    async def _owned(self, db: AsyncSession, owner_id: int, runner_id: str, *,
                     lock: bool = False, include_deleted: bool = False) -> Runner:
        query = select(Runner).where(Runner.runner_id == runner_id, Runner.owner_user_id == owner_id)
        if not include_deleted:
            query = query.where(Runner.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        runner = await db.scalar(query)
        if runner is None:
            raise APIError(404, "runner_not_found", "Runner 不存在")
        return runner

    async def create(self, owner_id: int, auth_version: int, name: str) -> RunnerCreateResponse:
        secret = new_token()
        issued_at = datetime.now(timezone.utc)
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            runner = Runner(owner_user_id=owner_id, name=name, created_at=issued_at, updated_at=issued_at)
            credential = RunnerCredential(runner=runner, token_hash=bytes.fromhex(token_digest(secret)),
                                          name="首次创建", created_at=issued_at)
            db.add(credential)
            await db.flush()
            result = RunnerCreateResponse(
                runner=RunnerCreated.model_validate(runner),
                credential=CredentialIssued(id=credential.id, token_id=credential.token_id,
                                            token=f"efr.{credential.token_id}.{secret}", expires_at=credential.expires_at),
                connection=RunnerEndpoint(websocket_url=self.websocket_url),
            )
        # 事务提交后才把完整 Token 交给 HTTP 层；失败时不会返回凭证。
        return result

    async def list(self, owner_id: int, page: int, pagesize: int) -> RunnerPage:
        visible = (Runner.owner_user_id == owner_id, Runner.deleted_at.is_(None))
        async with self.database.transaction() as db:
            total = await db.scalar(select(func.count()).select_from(Runner).where(*visible))
            rows = await db.scalars(select(Runner).where(*visible)
                                    .order_by(Runner.created_at.desc(), Runner.runner_id.desc())
                                    .offset((page - 1) * pagesize).limit(pagesize))
            items = [RunnerResponse.model_validate(row) for row in rows]
        states = await self.connections.read([row.runner_id for row in items])
        for item, state in zip(items, states):
            item.connection = state
        return RunnerPage(items=items, page=page, pagesize=pagesize, total=total)

    async def get(self, owner_id: int, runner_id: str) -> RunnerResponse:
        async with self.database.transaction() as db:
            result = RunnerResponse.model_validate(await self._owned(db, owner_id, runner_id))
        result.connection = (await self.connections.read([runner_id]))[0]
        return result

    async def rename(self, owner_id: int, auth_version: int, runner_id: str, name: str) -> RunnerResponse:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            runner = await self._owned(db, owner_id, runner_id, lock=True)
            runner.name = name
            await db.flush()
            await db.refresh(runner, attribute_names=["updated_at"])
            result = RunnerResponse.model_validate(runner)
        result.connection = (await self.connections.read([runner_id]))[0]
        return result

    async def delete(self, owner_id: int, auth_version: int, runner_id: str) -> None:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            runner = await self._owned(db, owner_id, runner_id, lock=True, include_deleted=True)
            if runner.deleted_at is None:
                now = datetime.now(timezone.utc)
                runner.deleted_at, runner.enabled = now, False
                await db.execute(update(RunnerCredential).where(RunnerCredential.runner_id == runner_id,
                                                                RunnerCredential.revoked_at.is_(None))
                                 .values(revoked_at=now))
        # 即使此前已经删除，也重试清理；Redis 故障不回滚已提交的凭证撤销。
        await self.connections.invalidate(runner_id)
