"""导入首个数据集版本，以及数据集归属检查。"""

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion

from ..dataset_import import ExampleDraft
from ..errors import APIError
from ..schemas.datasets import DatasetImportOptions, DatasetImportResult
from .auth import locked_user


async def require_owned_dataset(db: AsyncSession, owner_id: int, dataset_id: int, *, lock: bool = False) -> None:
    query = select(Dataset.id).where(Dataset.id == dataset_id, Dataset.user_id == owner_id)
    if lock:
        query = query.with_for_update(read=True)
    if await db.scalar(query) is None:
        raise APIError(404, "dataset_not_found", "数据集不存在")


class DatasetService:
    def __init__(self, database: Database):
        self.database = database

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
