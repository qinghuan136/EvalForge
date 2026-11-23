"""管理私有评分器、当前草稿及只追加的发布历史。"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from evalforge.db import Database
from evalforge.db.models import CodeEvaluator, CodeEvaluatorVersion, Evaluator

from ..errors import APIError
from ..evaluators.builtin import list_builtin_evaluators
from ..schemas.code_evaluators import (
    CodeEvaluatorCreate, CodeEvaluatorDetail, CodeEvaluatorDraftSaved, CodeEvaluatorPage, CodeEvaluatorPatch,
    CodeEvaluatorSummary, CodeEvaluatorVersionDetail, CodeEvaluatorVersionPage, CodeEvaluatorVersionSummary,
)
from .auth import locked_user

VERSION_COLUMNS = (CodeEvaluatorVersion.id, CodeEvaluatorVersion.version_number, CodeEvaluatorVersion.created_at)


def _versions(evaluator_id: int):
    return CodeEvaluatorVersion.evaluator_id == evaluator_id


def _name_conflict() -> APIError:
    return APIError(409, "evaluator_name_conflict", "评分器名称已被占用")


def _check_reserved_name(name: str | None) -> None:
    if any(name == rule.key for rule in list_builtin_evaluators()):
        raise _name_conflict()


class CodeEvaluatorService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    async def _owned(db: AsyncSession, owner_id: int, evaluator_id: int, *, lock: bool = False) -> CodeEvaluator:
        query = select(CodeEvaluator).where(CodeEvaluator.id == evaluator_id, CodeEvaluator.user_id == owner_id)
        row = await db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise APIError(404, "code_evaluator_not_found", "自定义评分器不存在")
        return row

    async def _editable(self, db: AsyncSession, owner_id: int, evaluator_id: int) -> CodeEvaluator:
        row = await self._owned(db, owner_id, evaluator_id, lock=True)
        if row.archived_at is not None:
            raise APIError(409, "code_evaluator_archived", "评分器已归档")
        return row

    @asynccontextmanager
    async def _named_transaction(self, name: str | None):
        try:
            async with self.database.transaction() as db:
                yield db
        except IntegrityError:
            # The unique constraint also covers concurrent writes from different owners.
            async with self.database.transaction() as db:
                if name is not None and await db.scalar(select(Evaluator.id).where(Evaluator.name == name)) is not None:
                    raise _name_conflict() from None
            raise

    @staticmethod
    async def _detail(db: AsyncSession, row: CodeEvaluator) -> CodeEvaluatorDetail:
        latest = (await db.execute(select(*VERSION_COLUMNS).where(_versions(row.id))
                                   .order_by(CodeEvaluatorVersion.version_number.desc()).limit(1))).one_or_none()
        return CodeEvaluatorDetail(id=row.id, name=row.name, description=row.description,
                                   draft_source_code=row.draft_source_code, archived_at=row.archived_at,
                                   latest_version=CodeEvaluatorVersionSummary.model_validate(latest) if latest else None)

    async def create(self, owner_id: int, auth_version: int, data: CodeEvaluatorCreate) -> CodeEvaluatorDetail:
        async with self._named_transaction(data.name) as db:
            await locked_user(db, owner_id, auth_version)
            _check_reserved_name(data.name)
            row = CodeEvaluator(user_id=owner_id, name=data.name, description=data.description)
            db.add(row)
            await db.flush()
            return CodeEvaluatorDetail(id=row.id, name=row.name, description=row.description,
                                       draft_source_code=None, latest_version=None, archived_at=None)

    async def list(self, owner_id: int, page: int, pagesize: int, published_only: bool, *,
                   include_archived: bool = False) -> CodeEvaluatorPage:
        scope = [CodeEvaluator.user_id == owner_id, CodeEvaluator.kind == "CODE"]
        latest_id = (select(CodeEvaluatorVersion.id).where(_versions(CodeEvaluator.id))
                     .order_by(CodeEvaluatorVersion.version_number.desc()).limit(1).correlate(CodeEvaluator)
                     .scalar_subquery())
        if published_only:
            scope.append(latest_id.is_not(None))
        if published_only or not include_archived:
            scope.append(CodeEvaluator.archived_at.is_(None))
        latest = aliased(CodeEvaluatorVersion)
        query = (select(CodeEvaluator.id, CodeEvaluator.name, CodeEvaluator.description, CodeEvaluator.archived_at,
                        latest.id.label("version_id"), latest.version_number, latest.created_at)
                 .select_from(CodeEvaluator).outerjoin(latest, latest.id == latest_id).where(*scope)
                 .order_by(CodeEvaluator.created_at.desc(), CodeEvaluator.id.desc())
                 .offset((page - 1) * pagesize).limit(pagesize))
        async with self.database.transaction() as db:
            total = await db.scalar(select(func.count()).select_from(CodeEvaluator).where(*scope))
            items = [CodeEvaluatorSummary(
                id=row.id, name=row.name, description=row.description, archived_at=row.archived_at,
                latest_version=CodeEvaluatorVersionSummary(id=row.version_id, version_number=row.version_number,
                                                          created_at=row.created_at) if row.version_id is not None else None,
            ) for row in await db.execute(query)]
            return CodeEvaluatorPage(items=items, page=page, pagesize=pagesize, total=total)

    async def get(self, owner_id: int, evaluator_id: int) -> CodeEvaluatorDetail:
        async with self.database.transaction() as db:
            return await self._detail(db, await self._owned(db, owner_id, evaluator_id))

    async def update(self, owner_id: int, auth_version: int, evaluator_id: int,
                     data: CodeEvaluatorPatch) -> CodeEvaluatorDetail:
        async with self._named_transaction(data.name) as db:
            await locked_user(db, owner_id, auth_version)
            row = await self._editable(db, owner_id, evaluator_id)
            _check_reserved_name(data.name)
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(row, field, value)
            await db.flush()
            return await self._detail(db, row)

    async def save_draft(self, owner_id: int, auth_version: int, evaluator_id: int,
                         source: str) -> CodeEvaluatorDraftSaved:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            row = await self._editable(db, owner_id, evaluator_id)
            row.draft_source_code = source
            return CodeEvaluatorDraftSaved(draft_source_code=source)

    async def archive(self, owner_id: int, auth_version: int, evaluator_id: int) -> CodeEvaluatorDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            row = await self._owned(db, owner_id, evaluator_id, lock=True)
            if row.archived_at is None:
                row.archived_at = datetime.now(timezone.utc)
            return await self._detail(db, row)

    async def publish(self, owner_id: int, auth_version: int, evaluator_id: int) -> CodeEvaluatorVersionSummary:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            row = await self._editable(db, owner_id, evaluator_id)
            if not row.draft_source_code or not row.draft_source_code.strip():
                raise APIError(409, "draft_required", "请先保存非空草稿")
            number = await db.scalar(select(func.max(CodeEvaluatorVersion.version_number)).where(_versions(row.id)))
            version = CodeEvaluatorVersion(evaluator_id=row.id, version_number=(number or 0) + 1,
                                           source_code=row.draft_source_code)
            db.add(version)
            await db.flush()
            return CodeEvaluatorVersionSummary.model_validate(version)

    async def list_versions(self, owner_id: int, evaluator_id: int,
                            page: int, pagesize: int) -> CodeEvaluatorVersionPage:
        async with self.database.transaction() as db:
            await self._owned(db, owner_id, evaluator_id)
            scope = _versions(evaluator_id)
            total = await db.scalar(select(func.count()).select_from(CodeEvaluatorVersion).where(scope))
            rows = await db.execute(select(*VERSION_COLUMNS).where(scope)
                                    .order_by(CodeEvaluatorVersion.version_number.desc())
                                    .offset((page - 1) * pagesize).limit(pagesize))
            return CodeEvaluatorVersionPage(items=[CodeEvaluatorVersionSummary.model_validate(row) for row in rows],
                                            total=total, page=page, pagesize=pagesize)

    async def get_version(self, owner_id: int, evaluator_id: int, version_id: int) -> CodeEvaluatorVersionDetail:
        async with self.database.transaction() as db:
            await self._owned(db, owner_id, evaluator_id)
            row = await db.scalar(select(CodeEvaluatorVersion).where(_versions(evaluator_id),
                                                                     CodeEvaluatorVersion.id == version_id))
            if row is None:
                raise APIError(404, "code_evaluator_version_not_found", "发布版本不存在")
            return CodeEvaluatorVersionDetail.model_validate(row)
