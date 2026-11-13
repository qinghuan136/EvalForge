"""数据集导入、归属检查和版本快照查询。"""

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion

from ..dataset_import import ExampleDraft
from ..errors import APIError
from ..schemas.datasets import (
    DatasetDetail, DatasetExamplePage, DatasetExampleResponse, DatasetImportOptions,
    DatasetImportResult, DatasetPage, DatasetSummary,
)
from .auth import locked_user


async def require_owned_dataset(db: AsyncSession, owner_id: int, dataset_id: int, *, lock: bool = False) -> None:
    query = select(Dataset.id).where(Dataset.id == dataset_id, Dataset.user_id == owner_id)
    if lock:
        query = query.with_for_update(read=True)
    if await db.scalar(query) is None:
        raise APIError(404, "dataset_not_found", "数据集不存在")


def dataset_snapshot(dataset_id: int, version_id: int):
    revision = DatasetExampleRevision
    ranked = (
        select(revision.dataset_example_id, revision.id.label("revision_id"), revision.revision_kind,
               func.row_number().over(partition_by=revision.dataset_example_id,
                                      order_by=revision.dataset_version_id.desc()).label("rank"))
        .join(DatasetExample, DatasetExample.id == revision.dataset_example_id)
        .join(DatasetVersion, DatasetVersion.id == revision.dataset_version_id)
        .where(DatasetExample.dataset_id == dataset_id, DatasetVersion.dataset_id == dataset_id,
               revision.dataset_version_id <= version_id)
        .subquery()
    )
    # 先选最新修订再排除 DELETE，否则会复活已删除的样本。
    return (select(ranked.c.dataset_example_id, ranked.c.revision_id)
            .where(ranked.c.rank == 1, ranked.c.revision_kind != "DELETE"))


def _dataset_summary_query(owner_id: int):
    latest = (select(func.max(DatasetVersion.id)).where(DatasetVersion.dataset_id == Dataset.id)
              .scalar_subquery().label("latest_version_id"))
    return (select(Dataset.id, Dataset.name, Dataset.description, Dataset.created_at, Dataset.updated_at, latest)
            .where(Dataset.user_id == owner_id))


class DatasetService:
    def __init__(self, database: Database):
        self.database = database

    async def list_datasets(self, owner_id: int, page: int, pagesize: int) -> DatasetPage:
        async with self.database.transaction() as db:
            total = await db.scalar(select(func.count()).select_from(Dataset).where(Dataset.user_id == owner_id))
            rows = (await db.execute(_dataset_summary_query(owner_id)
                .order_by(Dataset.created_at.desc(), Dataset.id.desc())
                .offset((page - 1) * pagesize).limit(pagesize))).mappings()
            return DatasetPage(items=[DatasetSummary.model_validate(row) for row in rows],
                               page=page, pagesize=pagesize, total=total)

    async def get(self, owner_id: int, dataset_id: int) -> DatasetDetail:
        async with self.database.transaction() as db:
            row = (await db.execute(_dataset_summary_query(owner_id)
                .add_columns(Dataset.metadata_.label("metadata")).where(Dataset.id == dataset_id))).mappings().one_or_none()
            if row is None:
                raise APIError(404, "dataset_not_found", "数据集不存在")
            count = 0
            if row["latest_version_id"] is not None:
                snapshot = dataset_snapshot(dataset_id, row["latest_version_id"]).subquery()
                count = await db.scalar(select(func.count()).select_from(snapshot))
            return DatasetDetail(**row, example_count=count)

    async def list_examples(self, owner_id: int, dataset_id: int, version_id: int | None,
                            page: int, pagesize: int) -> DatasetExamplePage:
        async with self.database.transaction() as db:
            await require_owned_dataset(db, owner_id, dataset_id)
            versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset_id)
            if version_id is None:
                version = await db.scalar(versions.order_by(DatasetVersion.id.desc()).limit(1))
            else:
                version = await db.scalar(versions.where(DatasetVersion.id == version_id))
                if version is None:
                    raise APIError(404, "dataset_version_not_found", "数据集版本不存在")

            items = []
            total = 0
            if version is not None:
                snapshot = dataset_snapshot(dataset_id, version).subquery()
                total = await db.scalar(select(func.count()).select_from(snapshot))
                revision = DatasetExampleRevision
                rows = (await db.execute(select(
                    snapshot.c.dataset_example_id.label("example_id"),
                    revision.input, revision.output, revision.metadata_.label("metadata"),
                ).join(revision, revision.id == snapshot.c.revision_id)
                    .order_by(snapshot.c.dataset_example_id)
                    .offset((page - 1) * pagesize).limit(pagesize))).mappings()
                items = [DatasetExampleResponse.model_validate(row) for row in rows]
            return DatasetExamplePage(dataset_id=dataset_id, dataset_version_id=version, items=items,
                                      page=page, pagesize=pagesize, total=total)

    async def import_dataset(self, owner_id: int, auth_version: int, options: DatasetImportOptions,
                             drafts: list[ExampleDraft]) -> DatasetImportResult:
        try:
            async with self.database.transaction() as db:
                await locked_user(db, owner_id, auth_version)
                dataset = Dataset(name=options.name, description=options.description, user_id=owner_id)
                version = DatasetVersion(dataset=dataset, user_id=owner_id)
                db.add(version)
                await db.flush()
                examples = [DatasetExample(dataset_id=dataset.id) for _ in drafts]
                db.add_all(examples)
                await db.flush()
                await db.execute(insert(DatasetExampleRevision), [
                    {"dataset_example_id": example.id, "dataset_version_id": version.id, "revision_kind": "CREATE",
                     "input": draft.input, "output": draft.output, "metadata_": draft.metadata}
                    for example, draft in zip(examples, drafts)
                ])
                return DatasetImportResult(dataset_id=dataset.id, dataset_version_id=version.id,
                                           example_count=len(drafts))
        except IntegrityError:
            # 现有表的名称是全局唯一；沿用账号注册的处理方式，在回滚后确认冲突。
            async with self.database.transaction() as db:
                existing = await db.scalar(select(Dataset.id).where(Dataset.name == options.name))
            if existing is not None:
                raise APIError(409, "dataset_name_conflict", "数据集名称已被使用") from None
            raise
