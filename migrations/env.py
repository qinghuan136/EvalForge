"""Alembic 使用 asyncpg 连接；与应用共用配置和 ORM metadata。"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from evalforge.db import Base, DatabaseSettings

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    settings = DatabaseSettings.from_env()
    context.configure(
        url=settings.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = DatabaseSettings.from_env()
    engine = create_async_engine(
        settings.url, poolclass=pool.NullPool, connect_args={"timeout": 10},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
