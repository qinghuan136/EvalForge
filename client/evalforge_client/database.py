"""客户端异步 ORM 生命周期和短事务；打开数据库不会恢复任务状态。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import URL, event, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from .models import Base

SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    """数据库版本或结构不受支持；不猜测迁移、不删除数据。"""


class ClientDatabase:
    """一个进程、一个事件循环内共享；session() 为独立的串行短事务。"""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if not str(path).strip() or str(path) == ":memory:":
            raise ValueError("A persistent SQLite file path is required")
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be an integer between 0 and 60000")
        self.path = Path(path).expanduser().resolve()
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.path)),
            connect_args={"timeout": busy_timeout_ms / 1000},
            poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0,
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._open = False

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _):
            connection.isolation_level = None
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=FULL")
            finally:
                cursor.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    def _check_owner(self) -> None:
        if self._owner is asyncio.current_task():
            raise RuntimeError("Nested sessions or lifecycle changes inside a session are not supported")

    async def __aenter__(self) -> "ClientDatabase":
        self._check_owner()
        async with self._lock:
            if self._open:
                raise RuntimeError("Database is already open")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                async with self.engine.begin() as connection:
                    version = (await connection.exec_driver_sql("PRAGMA user_version")).scalar_one()
                    tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
                    if version == 0:
                        if "runner_attempts" in tables:
                            raise SchemaVersionError("Unversioned runner_attempts table requires explicit migration")
                        await connection.run_sync(Base.metadata.create_all)
                        await connection.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
                    elif version != SCHEMA_VERSION:
                        raise SchemaVersionError(f"Unsupported SQLite schema version: {version}")
                    else:
                        if "runner_attempts" not in tables:
                            raise SchemaVersionError("Missing runner_attempts table")
                        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("runner_attempts"))
                        if {column["name"] for column in columns} != set(Base.metadata.tables["runner_attempts"].columns.keys()):
                            raise SchemaVersionError("runner_attempts columns do not match schema version 1")
                self._open = True
            except BaseException:
                await self.engine.dispose()
                raise
        return self

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """正常退出提交，异常或取消回滚；事务内不要网络等待或手动提交。"""
        self._check_owner()
        async with self._lock:
            if not self._open:
                raise RuntimeError("Open the database with async with before using it")
            self._owner = asyncio.current_task()
            try:
                async with self._sessions.begin() as session:
                    yield session
            finally:
                self._owner = None

    async def close(self) -> None:
        self._check_owner()
        async with self._lock:
            await self.engine.dispose()
            self._open = False

    async def __aexit__(self, *exc) -> None:
        await self.close()
