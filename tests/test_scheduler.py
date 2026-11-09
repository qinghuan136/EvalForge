"""调度异常路径使用 SQLite 事务与可控的 Runner 回执。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from api_support import SQLiteDatabase
from evalforge.db.models import (
    Base, Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion, ExperimentRun, Runner, RunnerAttempt, User,
)
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.scheduler import Scheduler
from evalforge.server.schemas.experiments import ExperimentCreate
from evalforge.server.services.dispatch import DispatchService
from evalforge.server.services.experiments import ExperimentService


class Session:
    def __init__(self, identity, call):
        self.identity = identity
        self.peer = SimpleNamespace(call=call)
        self.can_dispatch = True
        self.close_code = None

    async def check_current(self):
        pass

    def request_close(self, code):
        self.can_dispatch = False
        self.close_code = code


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = SQLiteDatabase("sqlite+aiosqlite:///" + str(Path(temp.name) / "scheduler.sqlite"))
        self.addAsyncCleanup(self.database.close)
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.database.transaction() as db:
            user = User(username="owner", password_hash="unused")
            db.add(user)
            await db.flush()
            dataset = Dataset(name="tests", user_id=user.id)
            db.add(dataset)
            await db.flush()
            self.owner_id, self.dataset_id = user.id, dataset.id
            example, version = DatasetExample(dataset_id=dataset.id), DatasetVersion(dataset_id=dataset.id)
            db.add_all([example, version])
            await db.flush()
            db.add(DatasetExampleRevision(dataset_example_id=example.id, dataset_version_id=version.id,
                                          revision_kind="CREATE", input={"value": 1}, output={}))
        self.dispatch = DispatchService(self.database)
        self.sessions = []
        self.scheduler = Scheduler(SimpleNamespace(ready_sessions=lambda: [
            session for session in self.sessions if session.can_dispatch
        ]), self.dispatch, poll_interval=0.01, retry_delay=0.05)
        self.addAsyncCleanup(self.scheduler.close)

    async def create_experiment(self, runner_id, *, repetitions=1):
        async with self.database.transaction() as db:
            db.add(Runner(runner_id=runner_id, name=runner_id, owner_user_id=self.owner_id))
        detail = await ExperimentService(self.database).create(
            self.owner_id, 1, self.dataset_id, ExperimentCreate(name=runner_id, repetitions=repetitions,
                execution_config={"runner_id": runner_id, "agent_target_id": "agent"}),
        )
        identity = RunnerIdentity(runner_id, self.owner_id, 1, datetime.max.replace(tzinfo=timezone.utc), b"")
        return identity, detail.experiment_id

    async def test_busy_retries_after_delay_and_missing_target_fails_job(self):
        identity, experiment_id = await self.create_experiment("busy")
        calls, rejected = [], asyncio.Event()

        async def call(method, payload, *, timeout):
            calls.append((asyncio.get_running_loop().time(), payload))
            if len(calls) == 1:
                return {"accepted": False, "reason": "busy"}
            rejected.set()
            return {"accepted": False, "reason": "target_not_found"}

        self.sessions.append(Session(identity, call))
        self.scheduler.start()
        await asyncio.wait_for(rejected.wait(), 3)
        await self.scheduler.close()
        self.assertEqual(len(calls), 2)
        (first_at, first), (second_at, second) = calls
        self.assertGreaterEqual(second_at - first_at, self.scheduler.retry_delay)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        async with self.database.transaction() as db:
            attempts = (await db.scalars(select(RunnerAttempt).order_by(RunnerAttempt.id))).all()
            self.assertEqual([(row.status, row.rejection_reason) for row in attempts],
                             [("rejected", "busy"), ("rejected", "target_not_found")])
            job = await db.scalar(select(ExperimentRun))
            self.assertEqual((job.status, job.error["code"]), ("failed", "target_not_found"))
        detail = await ExperimentService(self.database).get(self.owner_id, experiment_id)
        self.assertEqual(detail.status, "completed")

    async def test_slow_runner_does_not_block_others_and_timeout_keeps_capacity(self):
        slow_id, slow_experiment = await self.create_experiment("slow", repetitions=2)
        fast_id, fast_experiment = await self.create_experiment("fast")
        slow_entered, expire, fast_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow(method, payload, *, timeout):
            slow_entered.set()
            await expire.wait()
            raise TimeoutError

        async def fast(method, payload, *, timeout):
            fast_entered.set()
            return {"accepted": True}

        slow_session = Session(slow_id, slow)
        self.sessions.append(slow_session)
        self.scheduler.start()
        try:
            await asyncio.wait_for(slow_entered.wait(), 3)
            self.sessions.append(Session(fast_id, fast))
            self.scheduler.wake()
            await asyncio.wait_for(fast_entered.wait(), 3)
        finally:
            expire.set()
        await self.scheduler.close()
        self.assertEqual(slow_session.close_code, 1011)
        self.assertIsNone(await self.dispatch.claim(slow_id))
        async with self.database.transaction() as db:
            attempts = {row.runner_id: row for row in (await db.scalars(select(RunnerAttempt))).all()}
            self.assertEqual((attempts["slow"].status, attempts["slow"].unknown_reason),
                             ("unknown", "dispatch_timeout"))
            self.assertEqual(attempts["fast"].status, "accepted")
        slow_detail = await ExperimentService(self.database).get(self.owner_id, slow_experiment)
        fast_detail = await ExperimentService(self.database).get(self.owner_id, fast_experiment)
        self.assertEqual((slow_detail.status, slow_detail.progress.unknown, slow_detail.progress.queued),
                         ("running", 1, 1))
        self.assertEqual((fast_detail.status, fast_detail.progress.assigned), ("running", 1))
