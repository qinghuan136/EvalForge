import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from evalforge_client.store import SQLiteDatabase


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "runner.sqlite3"

    async def create_table(self, database):
        async with database.transaction() as connection:
            async with connection.execute("CREATE TABLE items (id TEXT PRIMARY KEY, value TEXT)"):
                pass

    async def rows(self, database):
        async with database.transaction() as connection:
            async with connection.execute("SELECT id, value FROM items ORDER BY id") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def test_commit_survives_close_and_reopen(self):
        async with SQLiteDatabase(self.path) as database:
            await self.create_table(database)
            async with database.transaction() as connection:
                async with connection.execute("INSERT INTO items VALUES (?, ?)", ("one", "中文'正文")):
                    pass
        async with SQLiteDatabase(self.path) as reopened:
            self.assertEqual(await self.rows(reopened), [{"id": "one", "value": "中文'正文"}])

    async def test_error_rolls_back_entire_transaction_and_connection_still_works(self):
        async with SQLiteDatabase(self.path) as database:
            await self.create_table(database)
            with self.assertRaises(sqlite3.IntegrityError):
                async with database.transaction() as connection:
                    async with connection.execute("INSERT INTO items VALUES ('one', 'first')"):
                        pass
                    async with connection.execute("INSERT INTO items VALUES ('one', 'duplicate')"):
                        pass
            self.assertEqual(await self.rows(database), [])

    async def test_task_cancellation_rolls_back_before_next_transaction(self):
        inserted = asyncio.Event()
        async with SQLiteDatabase(self.path) as database:
            await self.create_table(database)

            async def write():
                async with database.transaction() as connection:
                    async with connection.execute("INSERT INTO items VALUES ('one', 'cancelled')"):
                        pass
                    inserted.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(write())
            await asyncio.wait_for(inserted.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(await self.rows(database), [])

    async def test_concurrent_transactions_do_not_share_rollback(self):
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async with SQLiteDatabase(self.path) as database:
            await self.create_table(database)

            async def first():
                with self.assertRaises(ValueError):
                    async with database.transaction() as connection:
                        async with connection.execute("INSERT INTO items VALUES ('first', 'rollback')"):
                            pass
                        entered.set()
                        await release.wait()
                        raise ValueError("rollback first only")

            async def second():
                second_started.set()
                async with database.transaction() as connection:
                    async with connection.execute("INSERT INTO items VALUES ('second', 'commit')"):
                        pass

            task_one = asyncio.create_task(first())
            await asyncio.wait_for(entered.wait(), 2)
            task_two = asyncio.create_task(second())
            await asyncio.wait_for(second_started.wait(), 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(task_one, task_two), 2)
            self.assertEqual(await self.rows(database), [{"id": "second", "value": "commit"}])

    async def test_cancellation_after_commit_does_not_imply_data_was_rolled_back(self):
        committed, release = asyncio.Event(), asyncio.Event()
        original_commit = aiosqlite.Connection.commit

        async def delayed_commit(connection):
            await original_commit(connection)
            committed.set()
            await release.wait()

        async with SQLiteDatabase(self.path) as database:
            await self.create_table(database)

            async def write():
                async with database.transaction() as connection:
                    async with connection.execute("INSERT INTO items VALUES ('one', 'committed')"):
                        pass

            with patch.object(aiosqlite.Connection, "commit", delayed_commit):
                task = asyncio.create_task(write())
                await asyncio.wait_for(committed.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(await self.rows(database), [{"id": "one", "value": "committed"}])

    async def test_configuration_and_no_business_tables_created(self):
        async with SQLiteDatabase(self.path, busy_timeout_ms=1234) as database:
            async with database.transaction() as connection:
                for pragma, expected in (("foreign_keys", 1), ("busy_timeout", 1234), ("synchronous", 2)):
                    async with connection.execute(f"PRAGMA {pragma}") as cursor:
                        self.assertEqual((await cursor.fetchone())[0], expected)
                async with connection.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
                    self.assertEqual(await cursor.fetchall(), [])

    async def test_nested_transaction_fails_fast_and_closed_database_rejects_access(self):
        database = SQLiteDatabase(self.path)
        with self.assertRaises(RuntimeError):
            async with database.transaction():
                pass
        async with database:
            async with database.transaction():
                with self.assertRaises(RuntimeError):
                    async with database.transaction():
                        pass
                with self.assertRaises(RuntimeError):
                    await database.__aexit__(None, None, None)
        with self.assertRaises(RuntimeError):
            async with database.transaction():
                pass

    async def test_existing_non_database_file_is_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text("not a SQLite file", encoding="utf8")
        with self.assertRaises(sqlite3.DatabaseError):
            async with SQLiteDatabase(self.path):
                pass
        self.assertEqual(self.path.read_text(encoding="utf8"), "not a SQLite file")


if __name__ == "__main__":
    unittest.main()
