"""共享类型与 ORM 基类。结构参考 Phoenix；不依赖其业务模块。"""

from typing import Any

from sqlalchemy import JSON, DateTime, Integer, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# PostgreSQL 使用 JSONB；通用 JSON 变体供离线结构测试使用。
JSON_VALUE = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
UTC_TIMESTAMP = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    type_annotation_map = {dict[str, Any]: JSON_VALUE, list[dict[str, Any]]: JSON_VALUE}


class HasId(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


def deferred_reference(
    target: str, *, nullable: bool = True, index: bool = False,
    ondelete: str = "SET NULL", column_type=None,
):
    """保留外部引用字段，记录未来约束意图；当前不生成 FK 或外部占位表。"""
    return mapped_column(
        column_type if column_type is not None else Integer,
        nullable=nullable, index=index,
        info={"deferred_foreign_key": {"target": target, "ondelete": ondelete}},
        comment=f"Deferred FK: {target} (module not implemented)",
    )
