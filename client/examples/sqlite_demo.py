"""异步 SQLite 文件存储示例；使用独立示例表，不依赖会话或 Job。

在 client 目录运行：
    .venv/Scripts/python.exe -X utf8 examples/sqlite_demo.py
"""

import argparse
import asyncio
from pathlib import Path

from evalforge_client.store import SQLiteDatabase


async def run(path: Path, value: str) -> None:
    async with SQLiteDatabase(path) as database:
        async with database.transaction() as connection:
            async with connection.execute(
                "CREATE TABLE IF NOT EXISTS demo_values "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            ):
                pass
            async with connection.execute(
                "INSERT INTO demo_values (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("message", value),
            ):
                pass

    # 关闭后重新打开同一文件，确认读到的是已提交的持久化数据。
    async with SQLiteDatabase(path) as database:
        async with database.transaction() as connection:
            async with connection.execute(
                "SELECT value FROM demo_values WHERE key = ?", ("message",)
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("Stored value was not found after reopening the database")
                print(row["value"])


def main() -> None:
    parser = argparse.ArgumentParser(description="异步 SQLite 写入和持久化读取示例")
    parser.add_argument("--db", type=Path, default=Path("state/storage-demo.sqlite3"))
    parser.add_argument("--value", default="你好，SQLite！")
    args = parser.parse_args()
    asyncio.run(run(args.db, args.value))


if __name__ == "__main__":
    main()
