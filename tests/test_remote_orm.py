"""云端 Job/Attempt 的真实约束测试，不连接 PostgreSQL、不实现调度。"""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex

from evalforge.db.models import (
    Base, Dataset, DatasetExample, DatasetVersion, Experiment, ExperimentJob, ExperimentRun, RunnerAttempt,
    Runner, User,
)

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


class RemoteORMTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.counter = 0

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session, session.begin():
            owner = User(username="runner-owner", password_hash="test-only-hash")
            session.add_all([Runner(runner_id=key, name=key, owner=owner) for key in ("runner-01", "runner-02")])

    def seed_run(self, session):
        self.counter += 1
        dataset = Dataset(name=f"dataset-{self.counter}")
        version = DatasetVersion(dataset=dataset)
        example = DatasetExample(dataset=dataset)
        experiment = Experiment(dataset=dataset, dataset_version=version, name="remote", repetitions=1,
                                execution_config={"runner_id": "runner-01", "agent_target_id": "agent", "timeout_seconds": 30})
        run = ExperimentRun(experiment=experiment, dataset_example=example, repetition_number=1)
        session.add(run)
        session.flush()
        return run

    def attempt(self, run, name="a1", runner="runner-01", **changes):
        return RunnerAttempt(experiment_run=run, attempt_id=name, runner_id=runner,
                             dispatch_json={"job_id": run.job_id, "attempt_id": name, "agent_target_id": "agent",
                                            "example_id": str(run.dataset_example_id), "input": None, "timeout_seconds": 30},
                             **changes)

    def result(self, run, attempt="a1", status="failed"):
        return {"job_id": run.job_id, "attempt_id": attempt, "status": status, "output": None,
                "duration_ms": 1, "http_status": None, "error": {"code": "timeout", "message": "Timed out"}}

    def test_queued_job_and_experiment_do_not_require_legacy_job(self):
        with Session(self.engine) as session, session.begin():
            run = self.seed_run(session)
            self.assertEqual(run.status, "queued")
            self.assertIsNone(run.output)
            self.assertIsNone(run.start_time)
            self.assertIsNone(run.latency_ms)
            self.assertEqual(str(UUID(run.job_id)), run.job_id)
            self.assertEqual(run.experiment.status, "pending")
            self.assertIsNone(run.experiment.job)
            self.assertEqual(run.experiment.execution_config["agent_target_id"], "agent")
            self.assertTrue(ExperimentJob.__table__.info["deprecated"])
            self.assertEqual(run.attempts, [])
            self.assertEqual(next(iter(RunnerAttempt.__table__.c.runner_id.foreign_keys)).target_fullname,
                             "runners.runner_id")

    def test_same_job_keeps_multiple_attempts_with_separate_results(self):
        with Session(self.engine) as session, session.begin():
            run = self.seed_run(session)
            first = self.attempt(run, status="failed", result_json=self.result(run), result_received_at=NOW)
            second = self.attempt(run, "a2", status="accepted")
            session.add_all([first, second])
            session.flush()
            run_id = run.id
        with Session(self.engine) as session:
            run = session.get(ExperimentRun, run_id)
            history = {attempt.attempt_id: attempt for attempt in run.attempts}
            self.assertEqual(set(history), {"a1", "a2"})
            self.assertEqual(history["a1"].result_json["error"]["code"], "timeout")
            self.assertIsNone(history["a2"].result_json)
            self.assertEqual(history["a2"].dispatch_json["job_id"], run.job_id)

    def test_only_one_active_attempt_per_runner_and_per_job_excluding_unknown(self):
        with Session(self.engine) as session, session.begin():
            first_run, second_run = self.seed_run(session), self.seed_run(session)
            first = self.attempt(first_run)
            session.add(first)
            session.flush()
            first_id, first_run_id, second_run_id = first.id, first_run.id, second_run.id
        for run_id, runner in ((second_run_id, "runner-01"), (first_run_id, "runner-02")):
            with Session(self.engine) as session:
                session.add(self.attempt(session.get(ExperimentRun, run_id), "a2", runner))
                with self.assertRaises(IntegrityError):
                    session.commit()
        with Session(self.engine) as session, session.begin():
            first = session.get(RunnerAttempt, first_id)
            first.status, first.unknown_reason = "unknown", "runner_reported_unknown"
        with Session(self.engine) as session, session.begin():
            session.add(self.attempt(session.get(ExperimentRun, second_run_id), "a2"))
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(RunnerAttempt)), 2)

    def test_terminal_job_preserves_json_null_and_client_duration_without_clock_fabrication(self):
        with Session(self.engine) as session, session.begin():
            run = self.seed_run(session)
            run.status, run.output, run.duration_ms, run.http_status = "succeeded", {"task_output": None}, 42.5, 200
            run_id = run.id
        with Session(self.engine) as session:
            run = session.get(ExperimentRun, run_id)
            self.assertEqual(run.output, {"task_output": None})
            self.assertIsNone(run.start_time)
            self.assertEqual(run.latency_ms, 42.5)
            self.assertEqual(session.scalar(select(ExperimentRun.latency_ms).where(ExperimentRun.id == run_id)), 42.5)
        fallback = ExperimentRun(start_time=NOW, end_time=NOW + timedelta(milliseconds=150))
        self.assertEqual(fallback.latency_ms, 150)
        sql = str(select(ExperimentRun.latency_ms).compile(dialect=postgresql.dialect())).lower()
        self.assertIn("coalesce", sql)
        self.assertIn("extract(epoch", sql)

    def test_job_constraints_reject_invalid_state_identity_timing_and_error(self):
        with Session(self.engine) as session, session.begin():
            run_id = self.seed_run(session).id
        invalid = ({"status": "INVALID"}, {"job_id": " "}, {"repetition_number": 0}, {"duration_ms": -1},
                   {"http_status": 99}, {"start_time": NOW, "end_time": NOW - timedelta(seconds=1)},
                   {"status": "succeeded"}, {"output": {"task_output": "uncommitted"}},
                   {"status": "failed", "output": {"task_output": None}})
        for changes in invalid:
            with self.subTest(changes=changes), Session(self.engine) as session:
                run = session.get(ExperimentRun, run_id)
                for key, value in changes.items():
                    setattr(run, key, value)
                with self.assertRaises(IntegrityError):
                    session.commit()
        with Session(self.engine) as session, session.begin():
            run = session.get(ExperimentRun, run_id)
            run.status, run.output = "failed", {"task_output": None}
            run.error = {"code": "timeout", "message": "Timed out"}
        with Session(self.engine) as session:
            self.assertEqual(session.get(ExperimentRun, run_id).error["code"], "timeout")

    def test_attempt_constraints_cover_identity_results_and_cancel_intent(self):
        with Session(self.engine) as session, session.begin():
            run_id = self.seed_run(session).id
        invalid = ({"attempt_id": " "}, {"runner_id": " "}, {"status": "INVALID"},
                   {"status": "succeeded"}, {"status": "unknown"}, {"status": "rejected"},
                   {"cancel_reason": "user_requested"}, {"result_received_at": NOW},
                   {"result_json": {"status": "succeeded"}})
        for changes in invalid:
            with self.subTest(changes=changes), Session(self.engine) as session:
                run = session.get(ExperimentRun, run_id)
                attempt = self.attempt(run)
                for key, value in changes.items():
                    setattr(attempt, key, value)
                session.add(attempt)
                with self.assertRaises(IntegrityError):
                    session.commit()

    def test_public_identifiers_are_unique_and_internal_foreign_keys_are_enforced(self):
        with Session(self.engine) as session, session.begin():
            first, second = self.seed_run(session), self.seed_run(session)
            session.add(self.attempt(first, status="rejected", rejection_reason="busy"))
            session.flush()
            second_id, first_job_id = second.id, first.job_id
        with Session(self.engine) as session:
            session.get(ExperimentRun, second_id).job_id = first_job_id
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.add(self.attempt(session.get(ExperimentRun, second_id)))
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.add(RunnerAttempt(experiment_run_id=9999, runner_id="r", dispatch_json={}))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_experiment_checks_cascade_and_partial_postgresql_indexes(self):
        with Session(self.engine) as session, session.begin():
            run = self.seed_run(session)
            session.add(self.attempt(run))
            experiment_id = run.experiment_id
        for changes in ({"repetitions": 0}, {"status": "INVALID"}):
            with Session(self.engine) as session:
                experiment = session.get(Experiment, experiment_id)
                for key, value in changes.items():
                    setattr(experiment, key, value)
                with self.assertRaises(IntegrityError):
                    session.commit()
        indexes = [str(CreateIndex(index).compile(dialect=postgresql.dialect()))
                   for index in RunnerAttempt.__table__.indexes if index.unique]
        self.assertEqual(len(indexes), 2)
        for sql in indexes:
            self.assertIn("WHERE status IN", sql)
            self.assertNotIn("'unknown'", sql)
        with Session(self.engine) as session, session.begin():
            session.delete(session.get(Experiment, experiment_id))
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(RunnerAttempt)), 0)
