"""Database storage contract, real framework save/load/fork, and optimistic rollback."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic_ai import DeferredToolRequests
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict, ConversationEntry, ConversationRecord
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.usage import RunUsage
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from api_support import SQLiteDatabase
from evalforge.db.models import Base, User, AssistantConversationEntry as Entry, AssistantConversationRecord as Record
from evalforge.server.assistant.conversation_store import DatabaseConversationStore


def message(text):
    return ModelRequest(parts=[UserPromptPart(text)], metadata={"evalforge": {"text": text}})


class ConversationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = SQLiteDatabase(f"sqlite+aiosqlite:///{Path(temporary.name) / 'store.sqlite'}")
        self.addAsyncCleanup(self.database.close)
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.database.transaction() as db:
            users = [User(username=name, password_hash="test") for name in ("alice", "bob")]
            db.add_all(users)
            await db.flush()
            self.owner, self.other = [user.id for user in users]
        self.store = DatabaseConversationStore(self.database, self.owner)
        self.foreign = DatabaseConversationStore(self.database, self.other)

    async def save(self, name="conversation", texts=("hello",)):
        conversation = Conversation(conversation_id=name, messages=[message(text) for text in texts])
        await self.store.save(conversation, scope=self.store.scope)
        return conversation

    async def counts(self):
        async with self.database.transaction() as db:
            return tuple([await db.scalar(select(func.count()).select_from(model)) for model in (Record, Entry)])

    async def test_full_framework_round_trip_incremental_save_and_new_backend(self):
        conversation = await self.save(texts=("first", "second"))
        fresh = DatabaseConversationStore(self.database, self.owner)
        loaded = await fresh.load(conversation.conversation_id, scope=fresh.scope)
        self.assertEqual(loaded.messages, conversation.messages)
        old_revision = loaded.revision
        loaded.messages.append(ModelResponse(parts=[TextPart("answer")], model_name="test"))
        loaded.usage = RunUsage(requests=3, input_tokens=10, output_tokens=20)
        entries = await fresh.save(loaded, scope=fresh.scope)
        self.assertEqual(len(entries), 1)
        self.assertNotEqual(loaded.revision, old_revision)
        self.assertEqual(await self.counts(), (1, 3))
        restored = await self.store.load("conversation", scope=self.store.scope)
        self.assertEqual(restored.usage, loaded.usage)
        self.assertEqual(restored.messages, loaded.messages)
        loaded.messages.clear()
        self.assertEqual(len((await fresh.load("conversation")).messages), 3)

    async def test_deferred_requests_survive_storage(self):
        call = ToolCallPart("RUNNER_create_runner", {"name": "local"}, tool_call_id="approval")
        conversation = Conversation(messages=[message("create runner"), ModelResponse(parts=[call])],
                                    deferred_tool_requests=DeferredToolRequests(approvals=[call]))
        await self.store.save(conversation, scope=self.store.scope)
        restored = await self.store.load(conversation.conversation_id)
        self.assertEqual(restored.deferred_tool_requests.approvals, [call])
        self.assertEqual(restored.deferred_tool_requests.build_results(approvals={"approval": True}).approvals,
                         {"approval": True})

    async def test_history_order_limits_and_single_recursive_statement(self):
        conversation = await self.save(texts=tuple(str(i) for i in range(30)))
        record = await self.store.read_record(conversation.conversation_id)
        statements = []

        def capture(conn, cursor, sql, parameters, context, executemany):
            statements.append(sql)
            # Compile the exact query for PostgreSQL as well as executing SQLite.
            if context.compiled is not None:
                context.compiled.statement.compile(dialect=postgresql.dialect())

        event.listen(self.database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            recent = await self.store.read_history(record.head_id, limit=3)
        finally:
            event.remove(self.database.engine.sync_engine, "before_cursor_execute", capture)
        self.assertEqual([entry.message for entry in recent], conversation.messages[-3:])
        self.assertEqual(len(statements), 1)
        self.assertIn("WITH RECURSIVE", statements[0])
        all_entries = await self.store.read_history(record.head_id)
        self.assertEqual([entry.message for entry in all_entries], conversation.messages)
        self.assertEqual(await self.store.read_entry(record.head_id), all_entries[-1])
        self.assertEqual(await self.store.read_history(None), [])
        self.assertEqual(await self.store.read_history("missing", limit=0), [])
        with self.assertRaises(LookupError):
            await self.store.read_history("missing")
        with self.assertRaises(UserError):
            await self.store.read_history(record.head_id, limit=-1)

    async def test_owner_scope_list_and_cross_user_references(self):
        conversation = await self.save()
        source = await self.store.read_record(conversation.conversation_id)
        self.assertIsNone(await self.foreign.read_record(source.conversation_id))
        self.assertEqual(await self.foreign.read_records(), [])
        self.assertEqual(await self.store.read_records(scope=self.foreign.scope), [])
        for read in (self.foreign.read_entry(source.head_id), self.foreign.read_history(source.head_id)):
            with self.assertRaises(LookupError):
                await read
        malicious = replace(source, conversation_id="stolen", scope=self.foreign.scope)
        with self.assertRaises(ConversationConflict):
            await self.foreign.write_record(malicious, expected_revision=None)
        with self.assertRaises(ConversationConflict):
            await self.foreign.write_record(source, expected_revision=None)
        with self.assertRaises(ConversationConflict):
            await self.foreign.delete_record(source.conversation_id, expected_revision=source.revision)
        await self.save("newer")
        self.assertEqual([row.conversation_id for row in await self.store.read_records(limit=1)], ["newer"])
        self.assertEqual(await self.store.read_records(limit=0), [])

    async def test_stale_update_and_duplicate_create_roll_back_new_entries(self):
        conversation = await self.save()
        original = await self.store.read_record(conversation.conversation_id)
        await self.store.write_record(replace(original, revision="advanced"), expected_revision=original.revision)
        for expected in (original.revision, None):
            entry = ConversationEntry(parent_id=original.head_id, message=message("stale"))
            with self.assertRaises(ConversationConflict):
                await self.store.write_record(replace(original, head_id=entry.id, revision=str(uuid4())),
                                               new_entries=[entry], expected_revision=expected)
            self.assertEqual(await self.counts(), (1, 1))
        self.assertEqual((await self.store.read_record(original.conversation_id)).revision, "advanced")

    async def test_competing_writers_only_one_succeeds(self):
        conversation = await self.save()
        original = await self.store.read_record(conversation.conversation_id)

        async def write():
            entry = ConversationEntry(parent_id=original.head_id, message=message("new"))
            await DatabaseConversationStore(self.database, self.owner).write_record(
                replace(original, head_id=entry.id, revision=str(uuid4())),
                new_entries=[entry], expected_revision=original.revision)

        results = await asyncio.gather(write(), write(), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, ConversationConflict) for result in results), 1, results)
        self.assertEqual(await self.counts(), (1, 2))

    async def test_fork_branch_and_delete_keep_shared_history(self):
        original = await self.save(texts=("first", "second", "third"))
        fork = await self.store.fork(original.conversation_id, at=2, new_conversation_id="fork",
                                     scope=self.store.scope, source_scope=self.store.scope)
        self.assertEqual(await self.counts(), (2, 3))
        self.assertEqual(fork.messages, original.messages[:2])
        branch = await self.store.branch(original.conversation_id, at=1, scope=self.store.scope,
                                         expected_revision=original.revision)
        branch.messages.append(message("alternative"))
        await self.store.save(branch, scope=self.store.scope)
        self.assertEqual(await self.counts(), (2, 4))
        record = await self.store.read_record(original.conversation_id)
        self.assertTrue(record.branches)
        returned = await self.store.branch(original.conversation_id, from_entry=record.branches[0], scope=self.store.scope,
                                           expected_revision=record.revision)
        self.assertEqual(returned.messages, original.messages)
        await self.store.delete(original.conversation_id, scope=self.store.scope)
        self.assertEqual((await self.store.load("fork")).messages, fork.messages)
        self.assertEqual(await self.counts(), (1, 4))

    async def test_business_columns_survive_and_delete_checks_revision(self):
        conversation = await self.save()
        record = await self.store.read_record(conversation.conversation_id)
        run_id = uuid4()
        async with self.database.transaction() as db:
            await db.execute(update(Record).where(Record.conversation_id == record.conversation_id).values(
                title="kept", status="running", active_run_id=run_id,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=45), resume_model_name="model"))
        await self.store.write_record(replace(record, revision="updated"), expected_revision=record.revision)
        async with self.database.transaction() as db:
            row = await db.get(Record, record.conversation_id)
            self.assertEqual((row.title, row.active_run_id, row.status, row.resume_model_name),
                             ("kept", run_id, "running", "model"))
        with self.assertRaises(ConversationConflict):
            await self.store.delete_record(record.conversation_id, expected_revision=record.revision)
        await self.store.delete_record(record.conversation_id, expected_revision="updated")
        self.assertIsNone(await self.store.read_record(record.conversation_id))

    async def test_invalid_chain_and_immutable_entries(self):
        conversation = await self.save()
        record = await self.store.read_record(conversation.conversation_id)
        invalid = ConversationEntry(id="later", parent_id="missing", message=message("bad"))
        with self.assertRaises(ConversationConflict):
            await self.store.write_record(replace(record, head_id=invalid.id, revision="bad"),
                                           new_entries=[invalid], expected_revision=record.revision)
        duplicate = ConversationEntry(id=record.head_id, parent_id=None, message=message("overwrite"))
        with self.assertRaises(UserError):
            await self.store.write_record(replace(record, revision="bad"), new_entries=[duplicate],
                                           expected_revision=record.revision)
        self.assertEqual(await self.counts(), (1, 1))

    async def test_first_save_race_rolls_back_losing_entries(self):
        async def create():
            entry = ConversationEntry(parent_id=None, message=message("first"))
            record = ConversationRecord(conversation_id="same", head_id=entry.id, scope=self.store.scope)
            await DatabaseConversationStore(self.database, self.owner).write_record(
                record, new_entries=[entry], expected_revision=None)

        results = await asyncio.gather(create(), create(), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, ConversationConflict) for result in results), 1, results)
        self.assertEqual(await self.counts(), (1, 1))

    async def test_history_detects_external_corruption_and_accepts_delimiters_in_ids(self):
        first = ConversationEntry(id="a|b%", parent_id=None, message=message("first"))
        second = ConversationEntry(id="b%", parent_id=first.id, message=message("second"))
        record = ConversationRecord(conversation_id="chain", head_id=second.id, scope=self.store.scope)
        await self.store.write_record(record, new_entries=[first, second], expected_revision=None)
        self.assertEqual([entry.id for entry in await self.store.read_history(second.id)], [first.id, second.id])
        # Deliberately corrupt the immutable chain outside the backend.
        async with self.database.transaction() as db:
            await db.execute(update(Entry).where(Entry.id == first.id).values(parent_id=second.id))
        with self.assertRaisesRegex(LookupError, "Cycle"):
            await self.store.read_history(second.id)
        self.assertEqual([entry.id for entry in await self.store.read_history(second.id, limit=1)], [second.id])

    async def test_database_error_is_preserved_and_all_inserts_rollback(self):
        record = ConversationRecord(conversation_id="failed", head_id="entry", scope=self.store.scope)
        entry = ConversationEntry(id="entry", parent_id=None, message=message("new"))

        def fail(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO assistant_conversation_records"):
                raise OperationalError(statement, parameters, RuntimeError("forced storage failure"))

        event.listen(self.database.engine.sync_engine, "before_cursor_execute", fail)
        try:
            with self.assertRaises(OperationalError):
                await self.store.write_record(record, new_entries=[entry], expected_revision=None)
        finally:
            event.remove(self.database.engine.sync_engine, "before_cursor_execute", fail)
        self.assertEqual(await self.counts(), (0, 0))
