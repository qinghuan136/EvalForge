"""ConversationStore schema: shared history, lease checks, and additive migration."""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic_ai import DeferredToolRequests, ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from pydantic import TypeAdapter
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from evalforge.db.models import (
    AssistantConversationEntry as Entry, AssistantConversationRecord as Record,
    AssistantMessage, AssistantSession, Base, User,
)


class ConversationORMTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db, db.begin():
            user = User(username="conversation-owner", password_hash="test-only")
            db.add(user)
            db.flush()
            self.user_id = user.id

    def record(self, conversation_id, **values):
        return Record(conversation_id=conversation_id, user_id=self.user_id,
                      scope=f"user:{self.user_id}", revision="revision-1", **values)

    def test_messages_round_trip_with_metadata_and_shared_fork_history(self):
        original = ModelRequest(parts=[UserPromptPart("hello")], metadata={
            "evalforge": {"text": "hello", "page_context": {"type": "dataset", "id": 1}}})
        message = ModelMessagesTypeAdapter.dump_python([original], mode="json")[0]
        deferred = TypeAdapter(DeferredToolRequests).dump_python(DeferredToolRequests(approvals=[
            ToolCallPart("RUNNER_create_runner", {"name": "local"}, tool_call_id="call-1")]), mode="json")
        with Session(self.engine) as db, db.begin():
            db.add(Entry(id="entry-1", message=message))
            db.flush()
            db.add(Entry(id="entry-2", parent_id="entry-1", message=message))
            db.flush()
            db.add_all([self.record("source", head_id="entry-2", deferred_tool_requests=deferred,
                                    status="awaiting_approval", resume_model_name="selected-model"),
                        self.record("fork", head_id="entry-1", forked_from="source", forked_at="entry-1")])
        with Session(self.engine) as db, db.begin():
            source = db.get(Record, "source")
            self.assertEqual(source.usage, {})
            self.assertEqual(source.branches, [])
            self.assertEqual(source.format_version, 1)
            self.assertIsNone(source.active_run_id)
            self.assertEqual(ModelMessagesTypeAdapter.validate_python([db.get(Entry, "entry-1").message]), [original])
            restored = TypeAdapter(DeferredToolRequests).validate_python(source.deferred_tool_requests)
            self.assertEqual(restored.approvals[0].tool_call_id, "call-1")
            db.delete(source)
        with Session(self.engine) as db, db.begin():
            self.assertIsNone(db.get(Record, "source"))
            self.assertEqual(db.get(Record, "fork").forked_from, "source")
            self.assertEqual(len(db.scalars(select(Entry)).all()), 2)
            # Referenced histories survive record deletion, and cannot be deleted out from under a fork.
            with self.assertRaises(IntegrityError), db.begin_nested():
                db.delete(db.get(Entry, "entry-1"))
                db.flush()

    def test_scope_lease_status_and_reference_constraints(self):
        invalid = [
            {"scope": "user:someone-else"}, {"revision": " "}, {"title": " "},
            {"status": "other"}, {"status": "running"}, {"status": "awaiting_approval"},
            {"active_run_id": uuid4()}, {"lease_expires_at": datetime.now(timezone.utc)},
            {"active_run_id": uuid4(), "lease_expires_at": datetime.now(timezone.utc)},
            {"resume_model_name": " "}, {"format_version": 2},
            {"head_id": "missing"}, {"forked_at": "missing"},
        ]
        with Session(self.engine) as db, db.begin():
            for index, changes in enumerate(invalid):
                with self.subTest(changes=changes), self.assertRaises(IntegrityError), db.begin_nested():
                    record = self.record(f"invalid-{index}")
                    for name, value in changes.items():
                        setattr(record, name, value)
                    db.add(record)
                    db.flush()
            running = self.record("running", status="running", active_run_id=uuid4(),
                                  lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=45))
            db.add(running)
            db.flush()
            self.assertIsNotNone(running.active_run_id)
            with self.assertRaises(IntegrityError), db.begin_nested():
                db.add(Entry(id="self", parent_id="self", message={}))
                db.flush()
            with self.assertRaises(IntegrityError), db.begin_nested():
                db.add(Entry(id="child", parent_id="missing", message={}))
                db.flush()

    def test_owner_deletion_leaves_shared_entries_and_postgres_uses_expected_types(self):
        with Session(self.engine) as db, db.begin():
            db.add(Entry(id="entry", message={}))
            db.flush()
            db.add(self.record("conversation", head_id="entry"))
        with Session(self.engine) as db, db.begin():
            db.delete(db.get(User, self.user_id))
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Record, "conversation"))
            self.assertIsNotNone(db.get(Entry, "entry"))
        ddl = str(CreateTable(Record.__table__).compile(dialect=postgresql.dialect()))
        for expected in ("JSONB", "TIMESTAMP WITH TIME ZONE", "UUID", "ON DELETE RESTRICT"):
            self.assertIn(expected, ddl)
        self.assertEqual(set(Entry.__table__.c.keys()), {"id", "parent_id", "message", "kind"})


class ConversationMigrationTests(unittest.TestCase):
    def test_additive_upgrade_matches_orm_and_downgrade_preserves_legacy_tables(self):
        path = Path(__file__).resolve().parents[1] / "migrations/versions/20261118_conversation_store.py"
        spec = importlib.util.spec_from_file_location("conversation_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            for model in (User, AssistantSession, AssistantMessage):
                model.__table__.create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                schema = inspect(connection)
                for model in (Entry, Record):
                    actual = {column["name"]: column["nullable"] for column in schema.get_columns(model.__tablename__)}
                    self.assertEqual(actual, {column.name: column.nullable for column in model.__table__.c})
                    actual_checks = {check["sqltext"] for check in schema.get_check_constraints(model.__tablename__)}
                    expected_checks = {str(check.sqltext) for check in model.__table__.constraints
                                       if hasattr(check, "sqltext")}
                    self.assertEqual(actual_checks, expected_checks)
                    self.assertEqual({index["name"] for index in schema.get_indexes(model.__tablename__)},
                                     {index.name for index in model.__table__.indexes})
                module.downgrade()
            self.assertEqual(set(inspect(connection).get_table_names()), {"users", "assistant_sessions", "assistant_messages"})
