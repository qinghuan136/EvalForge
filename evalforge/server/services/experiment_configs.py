"""保存不可变实验配置版本及其评分器快照。"""

import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import DatasetVersion, ExperimentConfig, Runner

from ..errors import APIError
from ..schemas.experiments import ExperimentConfigDetail, ExperimentConfigPage, ExperimentConfigRevision, ExperimentConfigSave
from .auth import locked_user
from .datasets import require_owned_dataset
from .evaluators import build_experiment_evaluator_snapshots, load_evaluator_snapshots


def config_group(owner_id: int, group_id: UUID):
    return select(ExperimentConfig).where(
        ExperimentConfig.user_id == owner_id, ExperimentConfig.config_group_id == group_id,
    )


async def require_config_group(db: AsyncSession, owner_id: int, group_id: UUID, *,
                               lock: bool = False) -> ExperimentConfig:
    query = config_group(owner_id, group_id).where(ExperimentConfig.version == 1)
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise APIError(404, "experiment_config_not_found", "实验配置不存在")
    return row


async def require_runner(db: AsyncSession, owner_id: int, runner_id: str) -> Runner:
    row = await db.scalar(select(Runner).where(
        Runner.runner_id == runner_id, Runner.owner_user_id == owner_id, Runner.deleted_at.is_(None),
    ).with_for_update(read=True))
    if row is None:
        raise APIError(404, "runner_not_found", "Runner 不存在")
    return row


async def config_values(db: AsyncSession, owner_id: int, data: ExperimentConfigSave) -> dict:
    await require_owned_dataset(db, owner_id, data.dataset_id, lock=True)
    if data.dataset_version_id is not None:
        version = await db.scalar(select(DatasetVersion.id).where(
            DatasetVersion.id == data.dataset_version_id, DatasetVersion.dataset_id == data.dataset_id,
        ))
        if version is None:
            raise APIError(404, "dataset_version_not_found", "数据集版本不存在")
    await require_runner(db, owner_id, data.runner_id)
    snapshots = (build_experiment_evaluator_snapshots(data.evaluators) if data.evaluators
                 else await load_evaluator_snapshots(db, data.dataset_id, data.dataset_evaluator_ids))
    return {**data.model_dump(exclude={"evaluators", "dataset_evaluator_ids"}),
            "evaluators": [snapshot.model_dump(mode="json") for snapshot in snapshots]}


async def insert_config(db: AsyncSession, owner_id: int, values: dict, *,
                        group_id: UUID | None = None, version: int = 1) -> ExperimentConfig:
    row = ExperimentConfig(user_id=owner_id, config_group_id=group_id or uuid4(), version=version, **values)
    db.add(row)
    await db.flush()
    return row


async def _latest(db: AsyncSession, owner_id: int, group_id: UUID) -> ExperimentConfig:
    row = await db.scalar(config_group(owner_id, group_id).order_by(ExperimentConfig.version.desc()).limit(1))
    if row is None:
        raise APIError(404, "experiment_config_not_found", "实验配置不存在")
    return row


async def _page(db: AsyncSession, query, ordering: tuple, page: int, pagesize: int) -> ExperimentConfigPage:
    total = await db.scalar(query.with_only_columns(func.count(), maintain_column_froms=True))
    rows = await db.scalars(query.order_by(*ordering).offset((page - 1) * pagesize).limit(pagesize))
    return ExperimentConfigPage(items=[ExperimentConfigDetail.model_validate(row) for row in rows],
                                page=page, pagesize=pagesize, total=total)


class ExperimentConfigService:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, owner_id: int, auth_version: int, data: ExperimentConfigSave) -> ExperimentConfigDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            row = await insert_config(db, owner_id, await config_values(db, owner_id, data))
            return ExperimentConfigDetail.model_validate(row)

    async def revise(self, owner_id: int, auth_version: int, group_id: UUID,
                     data: ExperimentConfigRevision) -> tuple[ExperimentConfigDetail, bool]:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            await require_config_group(db, owner_id, group_id, lock=True)
            row = await _latest(db, owner_id, group_id)
            if row.version != data.base_version:
                raise APIError(409, "experiment_config_version_conflict", "配置已有新版本，请重新加载后编辑")
            values = await config_values(db, owner_id, data.config)
            previous = {name: getattr(row, name) for name in values}
            # JSON 中的 true 和 1 表示不同字面量。
            changed = json.dumps(previous, sort_keys=True) != json.dumps(values, sort_keys=True)
            if changed:
                row = await insert_config(db, owner_id, values, group_id=group_id, version=row.version + 1)
            return ExperimentConfigDetail.model_validate(row), changed

    async def get(self, owner_id: int, group_id: UUID) -> ExperimentConfigDetail:
        async with self.database.transaction() as db:
            return ExperimentConfigDetail.model_validate(await _latest(db, owner_id, group_id))

    async def list(self, owner_id: int, page: int, pagesize: int, dataset_id: int | None = None) -> ExperimentConfigPage:
        config = ExperimentConfig
        latest = (select(config.config_group_id, func.max(config.version).label("version"))
                  .where(config.user_id == owner_id).group_by(config.config_group_id).subquery())
        query = select(config).join(latest, (config.config_group_id == latest.c.config_group_id)
                                    & (config.version == latest.c.version)).where(config.user_id == owner_id)
        if dataset_id is not None:
            query = query.where(config.dataset_id == dataset_id)
        async with self.database.transaction() as db:
            if dataset_id is not None:
                await require_owned_dataset(db, owner_id, dataset_id)
            return await _page(db, query, (config.created_at.desc(), config.id.desc()), page, pagesize)

    async def versions(self, owner_id: int, group_id: UUID, page: int, pagesize: int) -> ExperimentConfigPage:
        async with self.database.transaction() as db:
            await require_config_group(db, owner_id, group_id)
            return await _page(db, config_group(owner_id, group_id), (ExperimentConfig.version.desc(),), page, pagesize)
