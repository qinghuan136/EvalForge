"""调度异常路径使用 SQLite 事务与可控的 Runner 回执。"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
from sqlalchemy import select

from api_support import SQLiteDatabase
from attempt_support import result_params
from evalforge.db.models import (
    Base, Dataset, DatasetExample, DatasetExampleRevision, DatasetVersion, Experiment, ExperimentRun,
    ExperimentRunAnnotation, Runner, RunnerAttempt, User,
)
from evalforge.server.runner_auth import RunnerIdentity
from evalforge.server.runner_connections import RegisteredConnection, RunnerConnections
from evalforge.server.scheduler import Scheduler
from evalforge.server.schemas.attempts import AttemptResult
from evalforge.server.schemas.evaluators import DatasetEvaluatorCreate
from evalforge.server.schemas.experiments import ExperimentCreate
from evalforge.server.services.attempts import AttemptService
from evalforge.server.services.dispatch import DispatchService
from evalforge.server.services.evaluations import EvaluationService
from evalforge.server.services.evaluators import DatasetEvaluatorService
from evalforge.server.services.experiments import ExperimentService


class Session:
    def __init__(self, identity, call, *, supports_streaming=False):
        self.identity = identity
        self.session_id = identity.runner_id
        self.peer = SimpleNamespace(call=call)
        self.can_dispatch = True
        self.supports_streaming = supports_streaming
        self.close_code = None
        self.preview_target = None

    async def check_current(self):
        pass

    def set_preview_target(self, target):
        self.preview_target = target

    def clear_preview_target(self, key):
        if self.preview_target is not None and self.preview_target.key == (key.job_id, key.attempt_id):
            self.preview_target = None

    def request_close(self, code):
        self.can_dispatch = False
        self.close_code = code


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = SQLiteDatabase("sqlite+aiosqlite:///" + str(Path(temp.name) / "scheduler.sqlite"))
        self.addAsyncCleanup(self.database.close)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.addAsyncCleanup(redis.aclose)
        self.connections = RunnerConnections(redis)
        self.experiments = ExperimentService(self.database, self.connections)
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
                                          revision_kind="CREATE", input={"value": 1}, output={"answer": "你好"}))
        self.dispatch = DispatchService(self.database)
        self.sessions = []
        self.scheduler = self.make_scheduler()

    def make_scheduler(self, evaluations=None):
        scheduler = Scheduler(SimpleNamespace(ready_sessions=lambda: [
            session for session in self.sessions if session.can_dispatch
        ]), self.dispatch, evaluations=evaluations, poll_interval=0.01, retry_delay=0.05)
        self.addAsyncCleanup(scheduler.close)
        return scheduler

    async def create_experiment(self, runner_id, *, repetitions=1, evaluator_ids=(), stream_output=False):
        async with self.database.transaction() as db:
            db.add(Runner(runner_id=runner_id, name=runner_id, owner_user_id=self.owner_id))
        capabilities = ["http_json", "http_sse"] if stream_output else ["http_json"]
        await self.connections.register(runner_id, RegisteredConnection(
            session_id="initial", phase="ready", capabilities=capabilities,
        ), 60)
        detail = await self.experiments.create(
            self.owner_id, 1, self.dataset_id, ExperimentCreate(name=runner_id, repetitions=repetitions,
                dataset_evaluator_ids=list(evaluator_ids),
                execution_config={"runner_id": runner_id, "agent_target_id": "agent", "stream_output": stream_output}),
        )
        identity = RunnerIdentity(runner_id, self.owner_id, 1, datetime.max.replace(tzinfo=timezone.utc), b"")
        return identity, detail.experiment_id

    async def test_downgraded_runner_skips_older_streaming_job_then_upgrade_can_dispatch_it(self):
        identity, streaming_id = await self.create_experiment("switch", stream_output=True)
        normal = await self.experiments.create(self.owner_id, 1, self.dataset_id, ExperimentCreate(
            name="normal", execution_config={"runner_id": identity.runner_id, "agent_target_id": "agent"},
        ))
        async with self.database.transaction() as db:
            normal_experiment = await db.get(Experiment, normal.experiment_id)
            normal_experiment.execution_config = {k: v for k, v in normal_experiment.execution_config.items()
                                                  if k != "stream_output"}
            jobs = {job.experiment_id: job.job_id for job in await db.scalars(select(ExperimentRun))}

        old_call = AsyncMock(return_value={"accepted": True})
        old_session = Session(identity, old_call)
        await self.scheduler._dispatch_one(old_session)
        method, payload = old_call.call_args.args
        self.assertEqual((method, payload["job_id"]), ("jobs.dispatch", jobs[normal.experiment_id]))
        self.assertNotIn("stream_output", payload)
        result = AttemptResult.model_validate(result_params((payload["job_id"], payload["attempt_id"])))
        await AttemptService(self.database).complete(identity, result)

        await self.scheduler._dispatch_one(old_session)
        old_call.assert_awaited_once()
        detail = await self.experiments.get(self.owner_id, streaming_id)
        self.assertEqual((detail.progress.queued, detail.progress.assigned), (1, 0))

        new_call = AsyncMock(return_value={"accepted": True})
        await self.scheduler._dispatch_one(Session(identity, new_call, supports_streaming=True))
        method, payload = new_call.call_args.args
        self.assertEqual((method, payload["job_id"]), ("jobs.dispatch", jobs[streaming_id]))
        self.assertIs(payload["stream_output"], True)
        async with self.database.transaction() as db:
            attempt = await db.scalar(select(RunnerAttempt).where(RunnerAttempt.attempt_id == payload["attempt_id"]))
            self.assertEqual(attempt.dispatch_json, payload)

    async def create_metric(self, name, actual="$.output.reply"):
        config = DatasetEvaluatorCreate(name=name, evaluator_key="exact_match", input_mapping={
            "path_mapping": {"actual": actual, "expected": "$.reference.answer"},
        })
        return (await DatasetEvaluatorService(self.database).create(
            self.owner_id, 1, self.dataset_id, config,
        )).dataset_evaluator_id

    async def test_task_commit_schedules_eval_and_experiment_waits_for_score(self):
        metric_id = await self.create_metric("answer_match")
        identity, experiment_id = await self.create_experiment("scored", evaluator_ids=[metric_id])
        evaluation = EvaluationService(self.database)
        run_evaluation = evaluation.run
        find_missing = evaluation.find_missing
        scheduler = self.make_scheduler(evaluation)
        recovered, scored, release, persisted = (asyncio.Event() for _ in range(4))
        calls = []

        async def track_discovery(run_ids=None, **kwargs):
            items = await find_missing(run_ids, **kwargs)
            if run_ids is None:
                recovered.set()
            return items

        async def held_score(item):
            calls.append(item.key)
            scored.set()
            await release.wait()
            await run_evaluation(item)
            persisted.set()

        claim = await self.dispatch.next_action(identity)
        self.assertIsNotNone(claim)  # 评分配置快照不能阻止领取 Task。
        params = AttemptResult.model_validate(result_params((claim.key.job_id, claim.key.attempt_id)))
        service = AttemptService(self.database, scheduler.notify_eval)
        with patch.object(evaluation, "run", held_score), patch.object(evaluation, "find_missing", track_discovery):
            scheduler.start()  # 没有 ready Runner，Eval 仍可运行。
            await asyncio.wait_for(recovered.wait(), 5)
            await service.complete(identity, params)
            await asyncio.wait_for(scored.wait(), 5)
            detail = await self.experiments.get(self.owner_id, experiment_id)
            self.assertEqual((detail.status, detail.progress.succeeded), ("running", 1))
            await service.complete(identity, params)
            scheduler.notify_eval(calls[0][0])
            release.set()
            await asyncio.wait_for(persisted.wait(), 10)
        await scheduler.close()
        self.assertEqual(len(calls), 1)
        detail = await self.experiments.get(self.owner_id, experiment_id)
        self.assertEqual(detail.status, "completed")
        async with self.database.transaction() as db:
            score = await db.scalar(select(ExperimentRunAnnotation))
            self.assertEqual((score.name, score.score, score.error), ("answer_match", 1.0, None))

    async def test_startup_recovers_only_missing_evals_even_across_pages_of_one_run(self):
        metric_ids = [await self.create_metric("already_saved"), await self.create_metric("new_score"),
                      await self.create_metric("missing_path", "$.output.missing[0]")]
        identity, experiment_id = await self.create_experiment("recover", evaluator_ids=metric_ids)
        claim = await self.dispatch.next_action(identity)
        params = AttemptResult.model_validate(result_params((claim.key.job_id, claim.key.attempt_id)))
        await AttemptService(self.database).complete(identity, params)  # 模拟提交后未发通知。
        async with self.database.transaction() as db:
            job = await db.scalar(select(ExperimentRun))
            now = datetime.now(timezone.utc)
            db.add(ExperimentRunAnnotation(experiment_run_id=job.id, name="already_saved", annotator_kind="CODE",
                                           score=0, start_time=now, end_time=now))

        evaluation = EvaluationService(self.database)
        run_evaluation = evaluation.run
        scheduler = self.make_scheduler(evaluation)
        names, done = set(), asyncio.Event()

        async def track(item):
            await run_evaluation(item)
            names.add(item.name)
            if len(names) == 2:
                done.set()

        with patch.object(evaluation, "run", track), patch("evalforge.server.scheduler.EVAL_SCAN_BATCH", 1):
            scheduler.start()
            await asyncio.wait_for(done.wait(), 10)
        await scheduler.close()
        self.assertEqual(names, {"new_score", "missing_path"})
        async with self.database.transaction() as db:
            annotations = {row.name: (row.score, row.error) for row in
                           await db.scalars(select(ExperimentRunAnnotation))}
        self.assertEqual(annotations["already_saved"], (0, None))
        self.assertEqual(annotations["new_score"], (1, None))
        self.assertIsNone(annotations["missing_path"][0])
        self.assertIsNotNone(annotations["missing_path"][1])
        self.assertEqual(await evaluation.find_missing(), [])
        self.assertEqual((await self.experiments.get(self.owner_id, experiment_id)).status, "completed")

    async def test_score_prepared_before_cancellation_is_discarded_at_save(self):
        metric_id = await self.create_metric("answer_match")
        identity, experiment_id = await self.create_experiment("cancel-score", evaluator_ids=[metric_id])
        claim = await self.dispatch.next_action(identity)
        params = AttemptResult.model_validate(result_params((claim.key.job_id, claim.key.attempt_id)))
        await AttemptService(self.database).complete(identity, params)
        evaluation = EvaluationService(self.database)
        item, = await evaluation.find_missing()

        detail = await self.experiments.cancel(self.owner_id, 1, experiment_id)
        self.assertEqual(detail.status, "cancelled")
        await evaluation.run(item)
        async with self.database.transaction() as db:
            self.assertIsNone(await db.scalar(select(ExperimentRunAnnotation)))
        self.assertEqual(await evaluation.find_missing(), [])

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
        detail = await self.experiments.get(self.owner_id, experiment_id)
        self.assertEqual(detail.status, "completed")

    async def test_cancel_between_claim_and_send_settles_reserved_job_without_rpc(self):
        identity, experiment_id = await self.create_experiment("preflight", repetitions=2)
        service = self.experiments
        prepare_send = self.dispatch.prepare_send
        call = AsyncMock()

        async def cancel_before_send(identity, claim):
            detail = await service.cancel(self.owner_id, 1, experiment_id)
            self.assertEqual((detail.status, detail.progress.assigned), ("cancelling", 1))
            return await prepare_send(identity, claim)

        with patch.object(self.dispatch, "prepare_send", side_effect=cancel_before_send):
            await self.scheduler._dispatch_one(Session(identity, call))
        call.assert_not_awaited()
        detail = await service.get(self.owner_id, experiment_id)
        self.assertEqual((detail.status, detail.progress.cancelled), ("cancelled", 2))

    async def test_dispatch_rejection_during_cancel_does_not_requeue_job(self):
        identity, experiment_id = await self.create_experiment("in-flight")
        service = self.experiments

        async def call(method, payload, *, timeout):
            self.assertEqual(method, "jobs.dispatch")
            detail = await service.cancel(self.owner_id, 1, experiment_id)
            self.assertEqual(detail.status, "cancelling")
            return {"accepted": False, "reason": "busy"}

        await self.scheduler._dispatch_one(Session(identity, call))
        detail = await service.get(self.owner_id, experiment_id)
        self.assertEqual((detail.status, detail.progress.cancelled, detail.progress.queued), ("cancelled", 1, 0))

    async def test_slow_runner_does_not_block_others_and_timeout_releases_capacity(self):
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
        async with self.database.transaction() as db:
            attempts = {row.runner_id: row for row in (await db.scalars(select(RunnerAttempt))).all()}
            self.assertEqual((attempts["slow"].status, attempts["slow"].unknown_reason),
                             ("unknown", "dispatch_timeout"))
            self.assertEqual(attempts["fast"].status, "accepted")
            skipped = attempts["slow"].dispatch_json["job_id"]
        slow_detail = await self.experiments.get(self.owner_id, slow_experiment)
        fast_detail = await self.experiments.get(self.owner_id, fast_experiment)
        self.assertEqual((slow_detail.status, slow_detail.progress.unknown, slow_detail.progress.queued),
                         ("running", 1, 1))
        self.assertEqual((fast_detail.status, fast_detail.progress.assigned), ("running", 1))
        next_claim = await self.dispatch.next_action(slow_id)
        self.assertIsNotNone(next_claim)
        self.assertNotEqual(next_claim.key.job_id, skipped)
        result = AttemptResult.model_validate(result_params((next_claim.key.job_id, next_claim.key.attempt_id)))
        await AttemptService(self.database).complete(slow_id, result)
        slow_detail = await self.experiments.get(self.owner_id, slow_experiment)
        self.assertEqual((slow_detail.status, slow_detail.progress.unknown, slow_detail.progress.succeeded),
                         ("completed", 1, 1))
