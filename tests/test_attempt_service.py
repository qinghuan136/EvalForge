import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from api_support import SQLiteDatabase
from attempt_support import result_params, seed_attempt
from evalforge.db.models import Base, Experiment, ExperimentRun, Runner, RunnerAttempt, User
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.schemas.attempts import AttemptKey, AttemptResult, RunnerSync
from evalforge.server.services.attempts import AttemptError, AttemptService


class AttemptServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = SQLiteDatabase()
        self.addAsyncCleanup(self.database.close)
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.database.transaction() as db:
            user = User(username="owner", password_hash="test-only")
            db.add(user)
            await db.flush()
            self.identity = RunnerIdentity("r1", user.id, 1, datetime.now(timezone.utc) + timedelta(days=1), b"x" * 32)
            db.add(Runner(runner_id="r1", owner_user_id=user.id, name="runner"))
        self.key = await seed_attempt(self.database, self.identity.owner_user_id, "r1")
        self.service = AttemptService(self.database)

    def result(self, **changes):
        return AttemptResult.model_validate(result_params(self.key, **changes))

    def snapshot(self, status=None, pending=False):
        return RunnerSync(runner_id="r1", active_jobs=[] if status is None else [dict(
            job_id=self.key[0], attempt_id=self.key[1], status=status)],
            pending_results=[self.result()] if pending else [])

    async def rows(self):
        async with self.database.transaction() as db:
            return (await db.scalar(select(RunnerAttempt).where(RunnerAttempt.attempt_id == self.key[1])),
                    await db.scalar(select(ExperimentRun).where(ExperimentRun.job_id == self.key[0])))

    async def test_started_complete_and_duplicate_messages_preserve_result_and_timestamps(self):
        key = AttemptKey(job_id=self.key[0], attempt_id=self.key[1])
        await self.service.started(self.identity, key)
        first, job = await self.rows()
        self.assertEqual((first.status, job.status), ("running", "running"))
        await self.service.started(self.identity, key)
        self.assertEqual((await self.rows())[0].started_reported_at, first.started_reported_at)
        await self.service.complete(self.identity, self.result())
        terminal, job = await self.rows()
        self.assertEqual((terminal.status, job.status), ("succeeded", "succeeded"))
        self.assertEqual(job.output, {"task_output": {"reply": "你好"}})
        self.assertEqual((job.duration_ms, job.http_status), (10, 200))
        await self.service.complete(self.identity, self.result())
        await self.service.started(self.identity, key)
        self.assertEqual((await self.rows())[0].result_received_at, terminal.result_received_at)
        self.assertEqual((await self.rows())[1].status, "succeeded")

    async def test_result_without_started_and_null_or_scalar_output(self):
        await self.service.complete(self.identity, self.result(output=None))
        self.assertEqual((await self.rows())[1].output, {"task_output": None})

    async def test_failed_result_stores_structured_error(self):
        await self.service.complete(self.identity, self.result(status="failed", output=None, http_status=None,
                                                               error={"code": "timeout", "message": "Timeout"}))
        attempt, job = await self.rows()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error, attempt.result_json["error"])

    async def test_conflicting_result_does_not_overwrite(self):
        await self.service.complete(self.identity, self.result())
        with self.assertRaises(AttemptError):
            await self.service.complete(self.identity, self.result(output="different"))
        self.assertEqual((await self.rows())[0].result_json, self.result().model_dump())

    async def test_json_boolean_and_number_are_conflicting_results(self):
        await self.service.complete(self.identity, self.result(output={"nested": [True, False]}))
        with self.assertRaises(AttemptError):
            await self.service.complete(self.identity, self.result(output={"nested": [1, 0]}))

    async def test_runner_user_and_job_identity_are_all_checked(self):
        for identity in (replace(self.identity, runner_id="other"), replace(self.identity, owner_user_id=999)):
            with self.assertRaises(AttemptError):
                await self.service.complete(identity, self.result())
        wrong = self.result().model_copy(update={"job_id": "other"})
        with self.assertRaises(AttemptError):
            await self.service.complete(self.identity, wrong)
        async with self.database.transaction() as db:
            experiment = await db.scalar(select(Experiment))
            experiment.user_id = None
        with self.assertRaises(AttemptError):
            await self.service.complete(self.identity, self.result())

    async def test_transaction_failure_rolls_back_both_rows_without_retry(self):
        transaction = self.database.transaction
        calls = []
        @asynccontextmanager
        async def failing_transaction():
            calls.append(1)
            async with transaction() as db:
                yield db
                await db.flush()
                raise OperationalError("private sql", {}, Exception("secret"))
        with patch.object(self.database, "transaction", failing_transaction):
            with self.assertRaises(OperationalError):
                await self.service.complete(self.identity, self.result())
        self.assertEqual(len(calls), 1)
        attempt, job = await self.rows()
        self.assertEqual((attempt.status, job.status), ("assigned", "assigned"))
        self.assertIsNone(attempt.result_json)
        self.assertIsNone(job.output)

    async def test_sync_pending_only_requests_upload_and_duplicate_is_still_replayed(self):
        response = await self.service.sync(self.identity, self.snapshot(pending=True))
        self.assertEqual(response["jobs"][0]["action"], "retry_upload")
        self.assertIsNone((await self.rows())[0].result_json)
        await self.service.complete(self.identity, self.result())
        self.assertEqual(await self.service.sync(self.identity, self.snapshot(pending=True)), response)

    async def test_sync_accepted_and_running(self):
        response = await self.service.sync(self.identity, self.snapshot("accepted"))
        self.assertEqual(response["jobs"][0]["action"], "continue")
        self.assertEqual((await self.rows())[0].status, "accepted")
        await self.service.sync(self.identity, self.snapshot("running"))
        self.assertEqual([r.status for r in await self.rows()], ["running", "running"])

    async def test_unknown_and_missing_do_not_release_capacity_or_restart_task(self):
        response = await self.service.sync(self.identity, self.snapshot("unknown"))
        self.assertEqual(response["jobs"][0]["action"], "cancel")
        self.assertEqual([r.status for r in await self.rows()], ["unknown", "unknown"])
        await self.service.sync(self.identity, self.snapshot())
        self.assertEqual((await self.rows())[0].unknown_reason, "missing_from_client_snapshot")
        await self.service.started(self.identity, AttemptKey(job_id=self.key[0], attempt_id=self.key[1]))
        self.assertEqual((await self.rows())[0].status, "unknown")
        await self.service.complete(self.identity, self.result())
        self.assertEqual((await self.rows())[0].status, "succeeded")

    async def test_sync_cancel_intent_and_terminal_are_not_overwritten_by_running(self):
        async with self.database.transaction() as db:
            attempt = await db.scalar(select(RunnerAttempt))
            attempt.cancel_requested_at = datetime.now(timezone.utc)
        response = await self.service.sync(self.identity, self.snapshot("running"))
        self.assertEqual(response["jobs"][0]["action"], "cancel")
        self.assertEqual((await self.rows())[0].status, "running")
        await self.service.complete(self.identity, self.result())
        response = await self.service.sync(self.identity, self.snapshot("running"))
        self.assertEqual(response["jobs"][0]["action"], "cancel")
        self.assertEqual((await self.rows())[0].status, "succeeded")

    async def test_sync_unknown_is_absorbed_even_when_cancellation_is_already_requested(self):
        await self.service.sync(self.identity, self.snapshot("running"))
        async with self.database.transaction() as db:
            attempt = await db.scalar(select(RunnerAttempt))
            attempt.cancel_requested_at = datetime.now(timezone.utc)
            attempt.cancel_reason = "user_requested"
        response = await self.service.sync(self.identity, self.snapshot("unknown"))
        attempt, job = await self.rows()
        self.assertEqual(response["jobs"][0]["action"], "cancel")
        self.assertEqual((attempt.status, job.status), ("unknown", "unknown"))
        self.assertEqual(attempt.cancel_reason, "user_requested")

    async def test_sync_experiment_cancellation_returns_cancel_without_fabricating_result(self):
        async with self.database.transaction() as db:
            experiment = await db.scalar(select(Experiment))
            experiment.status = "cancelling"
        response = await self.service.sync(self.identity, self.snapshot("running"))
        attempt, job = await self.rows()
        self.assertEqual(response["jobs"][0]["action"], "cancel")
        self.assertEqual((attempt.status, job.status), ("running", "running"))
        self.assertIsNone(attempt.result_json)
        self.assertIsNotNone(attempt.cancel_requested_at)

    async def test_invalid_sync_rolls_back_earlier_reconciliation(self):
        snapshot = self.snapshot("running")
        snapshot.pending_results = [self.result().model_copy(update={"attempt_id": "not-found"})]
        with self.assertRaises(AttemptError):
            await self.service.sync(self.identity, snapshot)
        self.assertEqual((await self.rows())[0].status, "assigned")

    def test_result_and_snapshot_validation(self):
        for changes in ({"duration_ms": True}, {"duration_ms": float("inf")}, {"http_status": True},
                        {"output": {"bad": float("nan")}}, {"status": "failed"},
                        {"error": {"code": "x", "message": "x"}}, {"extra": "x"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.result(**changes)
        snapshot = {
            "runner_id": "r1", "active_jobs": [{"job_id": self.key[0], "attempt_id": self.key[1], "status": "running"}],
            "pending_results": [self.result().model_dump()]}
        with self.assertRaises(ValidationError):
            RunnerSync.model_validate(snapshot)
