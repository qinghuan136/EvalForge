"""参考 Phoenix：共享异步 Engine，每次操作使用独立 Session 和短事务。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import DatabaseSettings


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.engine = create_async_engine(
            settings.url,
            echo=settings.echo,
            pool_pre_ping=True,
            pool_recycle=3300,
            connect_args={"timeout": 10, "command_timeout": 30},
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        # 成功提交，异常回滚；退出时关闭 Session 并归还连接。
        async with self._sessions.begin() as session:
            yield session

    async def check_connection(self) -> str:
        async with self.engine.connect() as connection:
            name = await connection.scalar(text("SELECT current_database()"))
        return str(name)

    async def close(self) -> None:
        await self.engine.dispose()

    async def __aenter__(self) -> "Database":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
