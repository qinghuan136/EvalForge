"""User-scoped ConversationStore with atomic, optimistic writes and shared immutable entries."""

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import timezone

from pydantic import TypeAdapter
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.conversation_store import ConversationConflict, ConversationEntry, ConversationRecord, ConversationStore
from pydantic_ai.exceptions import UserError
from sqlalchemy import Text, cast, delete, func, literal, select, true, union, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import AssistantConversationEntry as Entry, AssistantConversationRecord as Record

_RECORD = TypeAdapter(ConversationRecord)
_RECORD_FIELDS = tuple(ConversationRecord.__dataclass_fields__)


def _record(row: Record) -> ConversationRecord:
    values = {name: getattr(row, name) for name in _RECORD_FIELDS}
    if values["updated_at"].tzinfo is None:
        values["updated_at"] = values["updated_at"].replace(tzinfo=timezone.utc)
    return _RECORD.validate_python(values)


def _entry(row) -> ConversationEntry:
    return ConversationEntry(id=row.id, parent_id=row.parent_id, kind=row.kind,
                             message=ModelMessagesTypeAdapter.validate_python([row.message])[0])


def _token(column):
    # Escape delimiters so arbitrary entry IDs cannot match a different path component.
    return literal("|") + func.replace(func.replace(column, "%", "%25"), "|", "%7C") + literal("|")


class DatabaseConversationStore(ConversationStore):
    """Bind one backend to an authenticated user. Deleting records retains shared entries.

    The assistant service manages title, execution status, leases and recovery settings.
    These storage primitives update only framework-owned columns on existing records.
    """

    def __init__(self, database: Database, user_id: int, *, session: AsyncSession | None = None):
        self.database = database
        self.user_id = user_id
        self.scope = f"user:{user_id}"
        self.session = session

    @asynccontextmanager
    async def _transaction(self):
        if self.session is not None:
            yield self.session
        else:
            async with self.database.transaction() as db:
                yield db

    def _accessible_entries(self, db: AsyncSession):
        records, entries = Record.__table__, Entry.__table__
        owned = records.c.user_id == self.user_id
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            branches = func.jsonb_array_elements_text(records.c.branches).table_valued("value").render_derived()
        else:
            branches = func.json_each(records.c.branches).table_valued("key", "value")
        roots = union(
            select(records.c.head_id.label("id")).where(owned),
            select(records.c.forked_at.label("id")).where(owned),
            select(branches.c.value.label("id")).select_from(records.join(branches, true())).where(owned),
        ).subquery()
        reachable = (select(entries.c.id, entries.c.parent_id)
                     .join(roots, entries.c.id == roots.c.id).cte("accessible_entries", recursive=True))
        # UNION terminates even if externally corrupted data contains a cycle.
        return reachable.union(select(entries.c.id, entries.c.parent_id)
                               .join(reachable, entries.c.id == reachable.c.parent_id))

    async def read_record(self, conversation_id: str) -> ConversationRecord | None:
        async with self._transaction() as db:
            row = await db.scalar(select(Record).where(
                Record.conversation_id == conversation_id, Record.user_id == self.user_id))
            return _record(row) if row is not None else None

    async def read_records(self, *, scope: str | None = None,
                           limit: int | None = None) -> Sequence[ConversationRecord]:
        if limit is not None and limit < 0:
            raise UserError("`limit` must not be negative.")
        if scope is not None and scope != self.scope:
            return []
        query = (select(Record).where(Record.user_id == self.user_id)
                 .order_by(Record.updated_at.desc(), Record.conversation_id.desc()).limit(limit))
        async with self._transaction() as db:
            return [_record(row) for row in await db.scalars(query)]

    async def write_record(self, record: ConversationRecord, *, new_entries: Sequence[ConversationEntry] = (),
                            expected_revision: str | None) -> None:
        if record.scope != self.scope:
            raise ConversationConflict(record.conversation_id)
        if record.revision == expected_revision:
            raise UserError("A save must advance the revision.")
        values = _RECORD.dump_python(record, mode="json")
        values["updated_at"] = record.updated_at
        entries = [(entry, ModelMessagesTypeAdapter.dump_python([entry.message], mode="json")[0])
                   for entry in new_entries]
        async with self._transaction() as db:
            insert_row = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            # Validate external references against this user's reachable history.
            new_ids = {entry.id for entry, _ in entries}
            if len(new_ids) != len(entries):
                raise UserError("Duplicate new entry IDs.")
            references = {record.head_id, record.forked_at, *record.branches}
            references.update(entry.parent_id for entry, _ in entries)
            external = references.difference(new_ids, {None})
            reachable = self._accessible_entries(db)
            allowed = set(await db.scalars(select(reachable.c.id).where(reachable.c.id.in_(external)))) if external else set()
            if allowed != external:
                raise ConversationConflict(record.conversation_id)
            available = allowed | {None}
            for entry, message in entries:
                if entry.parent_id not in available:
                    raise UserError("New entries must follow their parents and cannot form cycles.")
                stored = await db.scalar(insert_row(Entry).values(
                    id=entry.id, parent_id=entry.parent_id, message=message, kind=entry.kind,
                ).on_conflict_do_nothing(index_elements=[Entry.id]).returning(Entry.id))
                if stored is None:
                    raise UserError("Entries are immutable; an entry ID already exists.")
                available.add(entry.id)

            if expected_revision is None:
                statement = (insert_row(Record).values(**values, user_id=self.user_id)
                             .on_conflict_do_nothing(index_elements=[Record.conversation_id]))
            else:
                statement = update(Record).where(
                    Record.conversation_id == record.conversation_id, Record.user_id == self.user_id,
                    Record.revision == expected_revision,
                ).values(**{key: value for key, value in values.items() if key not in ("conversation_id", "scope")})
            saved = await db.scalar(statement.returning(Record.conversation_id))
            if saved is None:
                # Raising inside the transaction also removes the entries inserted above.
                raise ConversationConflict(record.conversation_id)

    async def delete_record(self, conversation_id: str, *, expected_revision: str) -> None:
        async with self._transaction() as db:
            deleted = await db.scalar(delete(Record).where(
                Record.conversation_id == conversation_id, Record.user_id == self.user_id,
                Record.revision == expected_revision,
            ).returning(Record.conversation_id))
            if deleted is None:
                raise ConversationConflict(conversation_id)

    async def read_entry(self, entry_id: str) -> ConversationEntry:
        async with self._transaction() as db:
            reachable = self._accessible_entries(db)
            row = await db.scalar(select(Entry).where(
                Entry.id == entry_id, Entry.id.in_(select(reachable.c.id))))
            if row is None:
                raise LookupError(entry_id)
            return _entry(row)

    async def read_history(self, head_id: str | None, *, limit: int | None = None) -> list[ConversationEntry]:
        if limit is not None and limit < 0:
            raise UserError("`limit` must not be negative.")
        if head_id is None or limit == 0:
            return []
        async with self._transaction() as db:
            entries = Entry.__table__
            accessible = self._accessible_entries(db)
            history = (select(entries, literal(0).label("depth"), cast(_token(entries.c.id), Text).label("path"),
                              literal(False).label("cycle"))
                       .where(entries.c.id == head_id, entries.c.id.in_(select(accessible.c.id)))
                       .cte("history", recursive=True))
            parent = entries.alias("parent")
            path_token = _token(parent.c.id)
            position = func.strpos if db.get_bind().dialect.name == "postgresql" else func.instr
            previous = (select(parent, (history.c.depth + 1).label("depth"),
                               (history.c.path + path_token).label("path"),
                               (position(history.c.path, path_token) > 0).label("cycle"))
                        .join(history, parent.c.id == history.c.parent_id).where(~history.c.cycle))
            if limit is not None:
                previous = previous.where(history.c.depth + 1 < limit)
            history = history.union_all(previous)
            rows = (await db.execute(select(history).order_by(history.c.depth.desc()))).all()
            if not rows:
                raise LookupError(head_id)
            oldest = rows[0]
            if oldest.cycle:
                raise LookupError("Cycle in conversation history")
            if oldest.parent_id is not None and (limit is None or len(rows) < limit):
                raise LookupError(oldest.parent_id)
            return [_entry(row) for row in rows]
