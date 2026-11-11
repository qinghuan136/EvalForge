import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from evalforge_client.attempts import AttemptConflict, AttemptRepository, InvalidAttemptState
from evalforge_client.database import ClientDatabase, SchemaVersionError
from evalforge_client.models import RunnerAttempt


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state" / "runner.sqlite3"

    @staticmethod
    async def accept(repo, attempt_id="a1", **changes):
        values = dict(job_id="j1", attempt_id=attempt_id, agent_target_id="order-agent",
                      example_id="e1", input_json={"message": "你好'", "options": {"b": 2, "a": 1}},
                      timeout_seconds=30)
        values.update(changes)
        return await repo.accept(**values)

    @staticmethod
    def result(attempt_id="a1", status="succeeded"):
        return dict(job_id="j1", attempt_id=attempt_id, status=status,
                    output={"reply": "中文"}, duration_ms=12, http_status=200, error=None)

    async def test_schema_matches_design_and_pragmas_and_survives_reopen(self):
        async with ClientDatabase(self.path, busy_timeout_ms=1234) as db:
            async with db.engine.connect() as conn:
                self.assertEqual((await conn.exec_driver_sql("PRAGMA user_version")).scalar_one(), 2)
                self.assertEqual((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one(), 1)
                self.assertEqual((await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar_one(), 1234)
                self.assertEqual((await conn.exec_driver_sql("PRAGMA synchronous")).scalar_one(), 2)
                columns = await conn.run_sync(lambda c: inspect(c).get_columns("runner_attempts"))
                self.assertEqual({c["name"] for c in columns}, set(RunnerAttempt.__table__.columns.keys()))
                self.assertEqual(len(columns), 18)
                self.assertEqual(await conn.run_sync(lambda c: inspect(c).get_table_names()), ["runner_attempts"])
            row = await self.accept(AttemptRepository(db))
            self.assertTrue(row.accepted_at.endswith("+00:00"))
        async with ClientDatabase(self.path) as db:
            row = await AttemptRepository(db).get("j1", "a1")
            self.assertEqual(row.input_json["message"], "你好'")
            self.assertEqual(row.execution_status, "accepted")

    async def test_v1_migration_preserves_pending_results_and_defaults_to_json(self):
        self.path.parent.mkdir()
        result = self.result()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("""CREATE TABLE runner_attempts (
                job_id TEXT, attempt_id TEXT, agent_target_id TEXT NOT NULL, example_id TEXT NOT NULL,
                input_json TEXT NOT NULL, timeout_seconds REAL NOT NULL, execution_status TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL, cancel_reason TEXT, result_json TEXT, delivery_status TEXT NOT NULL,
                unknown_reason TEXT, accepted_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                acknowledged_at TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (job_id, attempt_id)
            )""")
            conn.execute("""INSERT INTO runner_attempts VALUES (
                'j1', 'a1', 'order-agent', 'e1', 'null', 30, 'succeeded', 0, NULL, ?, 'pending',
                NULL, 'old', 'old', 'old', NULL, 'old'
            )""", (json.dumps(result),))
            conn.execute("PRAGMA user_version=1")
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            row, = await repo.list_pending_results()
            self.assertIs(row.stream_output, False)
            self.assertEqual(row.result_json, result)
            self.assertEqual((await repo.acknowledge("j1", "a1")).delivery_status, "acknowledged")
            self.assertIs((await self.accept(repo, "a2", stream_output=True)).stream_output, True)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    async def test_duplicate_dispatch_preserves_state_and_rejects_changed_parameters(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            original = await self.accept(repo)
            self.assertTrue(await repo.claim("j1", "a1"))
            duplicate = await self.accept(repo, input_json={"options": {"a": 1, "b": 2}, "message": "你好'"})
            self.assertEqual(duplicate.execution_status, "running")
            self.assertEqual(duplicate.accepted_at, original.accepted_at)
            for changes in ({"input_json": {}}, {"agent_target_id": "other"},
                            {"example_id": "e2"}, {"timeout_seconds": 31}, {"stream_output": True}):
                with self.subTest(changes=changes), self.assertRaises(AttemptConflict):
                    await self.accept(repo, **changes)
            self.assertEqual((await repo.get("j1", "a1")).input_json, original.input_json)

    async def test_independent_connections_can_only_claim_once(self):
        async with ClientDatabase(self.path) as first, ClientDatabase(self.path) as second:
            one, two = AttemptRepository(first), AttemptRepository(second)
            await self.accept(one)
            outcomes = await asyncio.gather(one.claim("j1", "a1"), two.claim("j1", "a1"))
            self.assertEqual(sorted(outcomes), [False, True])
            self.assertFalse(await two.claim("j1", "a1"))

    async def test_finish_and_ack_are_persistent_idempotent_and_immutable(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.accept(repo)
            await repo.claim("j1", "a1")
            finished = await repo.finish("j1", "a1", self.result())
            self.assertEqual(finished.delivery_status, "pending")
            self.assertEqual(len(await repo.list_pending_results()), 1)
            self.assertEqual(await repo.list_active(), [])
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            finished_again = await repo.finish("j1", "a1", self.result())
            self.assertEqual(finished.finished_at, finished_again.finished_at)
            with self.assertRaises(AttemptConflict):
                await repo.finish("j1", "a1", {**self.result(), "output": "changed"})
            acked = await repo.acknowledge("j1", "a1")
            again = await repo.acknowledge("j1", "a1")
            self.assertEqual(acked.acknowledged_at, again.acknowledged_at)
            await repo.finish("j1", "a1", self.result())
            self.assertEqual(await repo.list_pending_results(), [])
            self.assertEqual((await repo.get("j1", "a1")).delivery_status, "acknowledged")

    async def test_recovery_is_explicit_and_preserves_other_states(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            for attempt_id in ("running", "accepted", "done"):
                await self.accept(repo, attempt_id)
            await repo.claim("j1", "running")
            await repo.claim("j1", "done")
            await repo.finish("j1", "done", self.result("done"))
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            self.assertEqual((await repo.get("j1", "running")).execution_status, "running")
            self.assertEqual(await repo.recover_interrupted(), 1)
            self.assertEqual(await repo.recover_interrupted(), 0)
            unknown = await repo.get("j1", "running")
            self.assertEqual(unknown.execution_status, "unknown")
            self.assertEqual(unknown.unknown_reason, "client_restarted")
            self.assertFalse(await repo.claim("j1", "running"))
            with self.assertRaises(InvalidAttemptState):
                await repo.finish("j1", "running", self.result("running"))
            self.assertEqual([r.attempt_id for r in await repo.list_active()], ["accepted"])
            self.assertEqual([r.attempt_id for r in await repo.list_pending_results()], ["done"])

    async def test_cancellation_intent_blocks_claim_and_can_finish_before_execution(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.accept(repo)
            row = await repo.request_cancel("j1", "a1", "user_requested")
            self.assertEqual(row.execution_status, "accepted")
            self.assertEqual(row.cancel_requested, 1)
            self.assertFalse(await repo.claim("j1", "a1"))
            row = await repo.finish("j1", "a1", self.result(status="cancelled"))
            self.assertEqual(row.delivery_status, "pending")
            self.assertIsNone(row.started_at)

    async def test_invalid_input_and_state_do_not_create_or_change_records(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            for changes in ({"job_id": " "}, {"timeout_seconds": float("inf")},
                            {"timeout_seconds": True}, {"input_json": {1: "bad"}},
                            {"input_json": float("nan")}, {"input_json": {"bad": object()}}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    await self.accept(repo, **changes)
            self.assertEqual(await repo.list_active(), [])
            await self.accept(repo)
            with self.assertRaises(InvalidAttemptState):
                await repo.acknowledge("j1", "a1")
            with self.assertRaises(InvalidAttemptState):
                await repo.finish("j1", "a1", self.result())
            with self.assertRaises(ValueError):
                await repo.finish("j1", "a1", self.result("wrong"))
            for changes in ({"duration_ms": -1}, {"http_status": True}, {"error": "bad"}, {"status": "failed"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    await repo.finish("j1", "a1", {**self.result(), **changes})
            with self.assertRaises(KeyError):
                await repo.acknowledge("missing", "a1")
            self.assertIsNone(await repo.get("missing", "a1"))
            self.assertFalse(await repo.claim("missing", "a1"))

    async def test_database_constraints_reject_invalid_direct_orm_updates(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.accept(repo)
            invalid_changes = [dict(execution_status="invalid"), dict(cancel_requested=2),
                               dict(timeout_seconds=-1), dict(delivery_status="acknowledged"),
                               dict(result_json={}), dict(execution_status="succeeded"),
                               dict(execution_status="succeeded", delivery_status="pending",
                                    finished_at="2026-11-01T00:00:00+00:00", result_json=self.result("wrong"))]
            for changes in invalid_changes:
                with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                    async with db.session() as session:
                        row = await session.get(RunnerAttempt, ("j1", "a1"))
                        for key, value in changes.items():
                            setattr(row, key, value)
            self.assertEqual((await repo.get("j1", "a1")).execution_status, "accepted")

    async def test_exception_and_cancellation_roll_back_and_release_session(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            await self.accept(repo)

            async def modify(ready):
                async with db.session() as session:
                    row = await session.get(RunnerAttempt, ("j1", "a1"))
                    row.cancel_requested = 1
                    await session.flush()
                    ready.set()
                    await asyncio.Event().wait()

            ready = asyncio.Event()
            task = asyncio.create_task(modify(ready))
            await asyncio.wait_for(ready.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual((await repo.get("j1", "a1")).cancel_requested, 0)
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                async with db.session() as session:
                    row = await session.get(RunnerAttempt, ("j1", "a1"))
                    row.cancel_requested = 1
                    await session.flush()
                    raise RuntimeError("rollback")
            self.assertTrue(await repo.claim("j1", "a1"))

    async def test_nested_sessions_and_closed_database_fail_fast(self):
        db = ClientDatabase(self.path)
        with self.assertRaises(RuntimeError):
            async with db.session():
                pass
        async with db:
            async with db.session():
                with self.assertRaises(RuntimeError):
                    async with db.session():
                        pass
                with self.assertRaises(RuntimeError):
                    await db.close()
        with self.assertRaises(RuntimeError):
            async with db.session():
                pass

    async def test_unknown_version_and_unversioned_table_are_not_modified(self):
        self.path.parent.mkdir()
        for version in (0, 1, 2, 99):
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS runner_attempts (preserved TEXT)")
                conn.execute("DELETE FROM runner_attempts")
                conn.execute("INSERT INTO runner_attempts VALUES ('keep me')")
                conn.execute(f"PRAGMA user_version={version}")
            with self.assertRaises(SchemaVersionError):
                async with ClientDatabase(self.path):
                    pass
            with closing(sqlite3.connect(self.path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)
                self.assertEqual(conn.execute("SELECT * FROM runner_attempts").fetchall(), [("keep me",)])

    async def test_json_null_input_and_missing_result_identity(self):
        async with ClientDatabase(self.path) as db:
            repo = AttemptRepository(db)
            row = await self.accept(repo, input_json=None)
            self.assertIsNone(row.input_json)
            async with db.session() as session:
                self.assertEqual(await session.scalar(text("SELECT input_json FROM runner_attempts")), "null")
            with self.assertRaises(IntegrityError):
                async with db.session() as session:
                    row = await session.get(RunnerAttempt, ("j1", "a1"))
                    row.execution_status, row.delivery_status = "failed", "pending"
                    row.finished_at, row.result_json = "2026-11-01T00:00:00+00:00", {"status": "failed"}


if __name__ == "__main__":
    unittest.main()
