"""Ownership check shared by APIs scoped to a dataset."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db.models import Dataset

from ..errors import APIError


async def require_owned_dataset(db: AsyncSession, owner_id: int, dataset_id: int, *, lock: bool = False) -> None:
    query = select(Dataset.id).where(Dataset.id == dataset_id, Dataset.user_id == owner_id)
    if lock:
        query = query.with_for_update(read=True)
    if await db.scalar(query) is None:
        raise APIError(404, "dataset_not_found", "数据集不存在")
