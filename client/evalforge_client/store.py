"""本地 SQLite 基础设施；业务表与 Attempt/结果仓储后续加入。

一个 SQLiteDatabase 持有一条连接，事务块之间串行执行，防止不同协程
共享连接时互相提交或回滚。事务内只做短 SQL 操作，不等待网络调用。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite


class SQLiteDatabase:
    """用 async with 打开文件，用 transaction() 获得一次事务的连接。

    只在事务块内使用返回的连接，并关闭查询游标；不要手动提交、回滚、
    executescript 或将同一事务拆给其他任务。连接不提供到事务外。
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if not str(path).strip() or str(path) == ":memory:":
            raise ValueError("A persistent SQLite file path is required")
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be an integer between 0 and 60000")
        self.path = Path(path).expanduser().resolve()
        self._timeout = busy_timeout_ms / 1000
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None

    def _check_not_owner(self) -> None:
        if self._owner is asyncio.current_task():
            raise RuntimeError("Nested transactions or lifecycle changes inside a transaction are not supported")

    async def __aenter__(self) -> "SQLiteDatabase":
        self._check_not_owner()
        async with self._lock:
            if self._connection is not None:
                raise RuntimeError("Database is already open")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = aiosqlite.connect(self.path, timeout=self._timeout, isolation_level=None)
            try:
                await connection
                connection.row_factory = aiosqlite.Row
                for sql in ("PRAGMA foreign_keys=ON", "PRAGMA synchronous=FULL"):
                    async with connection.execute(sql):
                        pass
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._check_not_owner()
        async with self._lock:
            if self._connection is not None:
                try:
                    await self._connection.close()
                finally:
                    self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """短事务：成功提交，异常/取消时尝试回滚；SQLite 错误向上抛出。

        取消不保证未提交：commit 已被工作线程执行时无法撤销。
        后续业务写入须用唯一约束等机制保证重试幂等。
        """
        self._check_not_owner()
        async with self._lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Open the database with async with before using it")
            self._owner = asyncio.current_task()
            try:
                # 在执行前获取写锁；其他连接竞争时由 SQLite 的 busy timeout 处理。
                async with connection.execute("BEGIN IMMEDIATE"):
                    pass
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                self._owner = None
