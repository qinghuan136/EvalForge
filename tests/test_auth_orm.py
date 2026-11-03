"""身份存储约束测试；不实现签发、密码验证或 WebSocket 鉴权。"""

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from evalforge.db.models import (
    Base, Dataset, DatasetExample, DatasetVersion, Experiment, ExperimentRun,
    Runner, RunnerAttempt, RunnerCredential, User,
)

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
HASH = bytes(range(32))  # 测试摘要，不是真实凭证。


class AuthORMTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

    def seed_runner(self, session):
        owner = User(username="alice", password_hash="test-only-encoded-hash")
        runner = Runner(owner=owner, name="开发机")
        session.add(runner)
        session.flush()
        return runner

    def test_identity_credentials_round_trip_and_independent_rotation(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            first = RunnerCredential(runner=runner, token_hash=HASH, created_at=NOW, revoked_at=NOW)
            second = RunnerCredential(runner=runner, token_hash=HASH, created_at=NOW)
            session.add_all([first, second])
            session.flush()
            runner_id = runner.runner_id
            self.assertNotEqual(first.token_id, second.token_id)
            self.assertEqual(second.expires_at, NOW.replace(year=2036))
            self.assertTrue(runner.enabled)
            self.assertTrue(runner.owner.enabled)
            self.assertEqual(runner.owner.auth_version, 1)
        with Session(self.engine) as session:
            runner = session.get(Runner, runner_id)
            self.assertEqual(runner.owner.username, "alice")
            self.assertEqual(len(runner.credentials), 2)
            self.assertEqual(sum(row.revoked_at is None for row in runner.credentials), 1)
            self.assertEqual(runner.credentials[0].token_hash, HASH)

    def test_ten_calendar_years_default_and_explicit_expiry(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            leap = RunnerCredential(runner=runner, token_hash=HASH,
                                    created_at=datetime(2028, 2, 29, tzinfo=timezone.utc))
            automatic = RunnerCredential(runner=runner, token_hash=HASH)
            explicit = RunnerCredential(runner=runner, token_hash=HASH, created_at=NOW,
                                        expires_at=NOW + timedelta(days=1))
            session.add_all([leap, automatic, explicit])
            session.flush()
            self.assertEqual(leap.expires_at, datetime(2038, 2, 28, tzinfo=timezone.utc))
            self.assertEqual(automatic.expires_at.year, automatic.created_at.year + 10)
            self.assertEqual(explicit.expires_at, NOW + timedelta(days=1))

    def test_constraints_reject_invalid_verifiers_times_names_and_versions(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            runner_id, owner_id = runner.runner_id, runner.owner_user_id
        invalid = [
            User(username=" ", password_hash="hash"),
            User(username="bob", password_hash=" "),
            User(username="bob", password_hash="hash", auth_version=0),
            User(username="alice", password_hash="hash"),
            Runner(runner_id=" ", owner_user_id=owner_id, name="machine"),
            Runner(owner_user_id=owner_id, name=" "),
        ]
        for changes in ({"token_id": " "}, {"token_hash": b"short"},
                        {"expires_at": NOW}, {"expires_at": NOW - timedelta(days=1)},
                        {"revoked_at": NOW - timedelta(seconds=1)}):
            values = {"runner_id": runner_id, "token_hash": HASH, "created_at": NOW, **changes}
            invalid.append(RunnerCredential(**values))
        for row in invalid:
            with self.subTest(model=type(row).__name__), Session(self.engine) as session:
                session.add(row)
                with self.assertRaises(IntegrityError):
                    session.commit()

    def test_missing_owners_runners_and_duplicate_token_identifiers_are_rejected(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            session.add(RunnerCredential(runner=runner, token_id="public-lookup", token_hash=HASH))
            session.flush()
            runner_id, owner_id = runner.runner_id, runner.owner_user_id
        invalid = [
            Runner(owner_user_id=9999, name="orphan"),
            Runner(runner_id=runner_id, owner_user_id=owner_id, name="duplicate"),
            RunnerCredential(runner_id="missing", token_hash=HASH),
            RunnerCredential(runner_id=runner_id, token_id="public-lookup", token_hash=HASH),
        ]
        for row in invalid:
            with self.subTest(model=type(row).__name__), Session(self.engine) as session:
                session.add(row)
                with self.assertRaises(IntegrityError):
                    session.commit()

    def test_disabled_entities_keep_credentials_and_deletion_does_not_erase_history(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            session.add(RunnerCredential(runner=runner, token_hash=HASH))
            session.flush()
            runner_id, owner_id = runner.runner_id, runner.owner_user_id
        for model, key in ((User, owner_id), (Runner, runner_id)):
            with Session(self.engine) as session:
                row = session.get(model, key)
                # 加载关联也不能让 ORM 把外键置空或级联删除。
                list(row.runners if model is User else row.credentials)
                session.delete(row)
                with self.assertRaises(IntegrityError):
                    session.commit()
        with Session(self.engine) as session, session.begin():
            runner = session.get(Runner, runner_id)
            runner.enabled = runner.owner.enabled = False
        with Session(self.engine) as session:
            self.assertFalse(session.get(User, owner_id).enabled)
            self.assertIsNotNone(session.scalar(select(RunnerCredential)))

    def test_experiment_owner_and_attempt_runner_references_preserve_history(self):
        with Session(self.engine) as session, session.begin():
            runner = self.seed_runner(session)
            dataset = Dataset(name="test")
            experiment = Experiment(dataset=dataset, dataset_version=DatasetVersion(dataset=dataset),
                                    user=runner.owner, name="test", repetitions=1)
            run = ExperimentRun(experiment=experiment, dataset_example=DatasetExample(dataset=dataset), repetition_number=1)
            attempt = RunnerAttempt(experiment_run=run, runner=runner, dispatch_json={},
                                    status="rejected", rejection_reason="busy")
            session.add(attempt)
            session.flush()
            runner_id, experiment_id, attempt_id = runner.runner_id, experiment.id, attempt.id
        with Session(self.engine) as session:
            attempt = session.get(RunnerAttempt, attempt_id)
            self.assertEqual(attempt.runner.owner_user_id, attempt.experiment_run.experiment.user_id)
            session.delete(attempt.runner)
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.get(RunnerAttempt, attempt_id).runner_id = "missing"
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.get(Experiment, experiment_id).user_id = 9999
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session, session.begin():
            session.delete(session.get(Experiment, experiment_id))
        with Session(self.engine) as session:
            self.assertIsNone(session.get(RunnerAttempt, attempt_id))
            self.assertIsNotNone(session.get(Runner, runner_id))

    def test_experiment_remains_when_owner_without_runners_is_deleted(self):
        with Session(self.engine) as session, session.begin():
            owner = User(username="alice", password_hash="test-only-hash")
            dataset = Dataset(name="test")
            experiment = Experiment(dataset=dataset, dataset_version=DatasetVersion(dataset=dataset),
                                    user=owner, name="test", repetitions=1)
            session.add(experiment)
            session.flush()
            owner_id, experiment_id = owner.id, experiment.id
        with Session(self.engine) as session, session.begin():
            owner = session.get(User, owner_id)
            list(owner.experiments)
            session.delete(owner)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(Experiment, experiment_id).user_id)

    def test_postgresql_storage_types_and_foreign_key_indexes(self):
        sql = str(CreateTable(RunnerCredential.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("BYTEA", sql)
        self.assertIn("TIMESTAMP WITH TIME ZONE", sql)
        for model, column in ((Runner, "owner_user_id"), (RunnerCredential, "runner_id"),
                              (RunnerAttempt, "runner_id"), (Experiment, "user_id")):
            self.assertTrue(any(tuple(index.columns.keys()) == (column,) for index in model.__table__.indexes))
