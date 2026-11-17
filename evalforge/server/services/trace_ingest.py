"""Persist an authenticated OTLP batch in one transaction."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from evalforge.db import Database
from evalforge.db.models import Span, Trace

from ..otlp import OtlpRequestError, SpanValues

_BATCH_ROWS = 100
_TRACE_KEY = ("owner_user_id", "trace_id")
_SPAN_KEY = ("trace_row_id", "span_id")


def _batches(rows: list):
    for offset in range(0, len(rows), _BATCH_ROWS):
        yield rows[offset:offset + _BATCH_ROWS]


def _extend_bounds(bounds: dict, key, start, end) -> None:
    old_start, old_end = bounds.get(key, (start, end))
    bounds[key] = min(start, old_start), max(end, old_end)


class TraceIngestService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def ingest(self, owner_user_id: int, spans: list[SpanValues]) -> None:
        if not spans:
            return
        traces = Trace.__table__
        span_table = Span.__table__
        bounds = {}
        for span in spans:
            _extend_bounds(bounds, span["trace_id"], span["start_time_unix_nano"], span["end_time_unix_nano"])

        async with self.database.transaction() as db:
            sqlite = db.get_bind().dialect.name == "sqlite"
            insert = sqlite_insert if sqlite else pg_insert
            least, greatest = (func.min, func.max) if sqlite else (func.least, func.greatest)
            trace_rows = {}
            new_trace_rows = set()

            # Order all batches consistently for concurrent inserts touching the same traces.
            for ids in _batches(sorted(bounds)):
                rows = [_trace_values(owner_user_id, key, *bounds[key]) for key in ids]
                stmt = insert(traces).values(rows).on_conflict_do_nothing(index_elements=_TRACE_KEY)
                result = await db.execute(stmt.returning(traces.c.trace_id, traces.c.id))
                inserted = {trace_id: row_id for trace_id, row_id in result}
                trace_rows.update(inserted)
                new_trace_rows.update(inserted.values())
                missing = [key for key in ids if key not in inserted]
                if missing:
                    existing = await db.execute(select(traces.c.trace_id, traces.c.id).where(
                        traces.c.owner_user_id == owner_user_id, traces.c.trace_id.in_(missing),
                    ))
                    trace_rows.update({trace_id: row_id for trace_id, row_id in existing})
            if len(trace_rows) != len(bounds):
                raise OtlpRequestError(503, "Trace batch could not be stored; retry the request")

            trace_keys = {row_id: trace_id for trace_id, row_id in trace_rows.items()}
            added_bounds = {}
            ordered = sorted(spans, key=lambda span: (span["trace_id"], span["span_id"]))
            for batch in _batches(ordered):
                rows = []
                for span in batch:
                    values = span.copy()
                    values["trace_row_id"] = trace_rows[values.pop("trace_id")]
                    rows.append(values)
                stmt = insert(span_table).values(rows).on_conflict_do_nothing(index_elements=_SPAN_KEY)
                inserted = await db.execute(stmt.returning(
                    span_table.c.trace_row_id, span_table.c.start_time_unix_nano, span_table.c.end_time_unix_nano,
                ))
                for row_id, start, end in inserted:
                    if row_id not in new_trace_rows:
                        _extend_bounds(added_bounds, trace_keys[row_id], start, end)

            # Only new spans may widen an existing trace. Conflicting retries never change its bounds.
            for ids in _batches(sorted(added_bounds)):
                rows = [_trace_values(owner_user_id, key, *added_bounds[key]) for key in ids]
                stmt = insert(traces).values(rows)
                await db.execute(stmt.on_conflict_do_update(index_elements=_TRACE_KEY, set_={
                    "start_time_unix_nano": least(traces.c.start_time_unix_nano, stmt.excluded.start_time_unix_nano),
                    "end_time_unix_nano": greatest(traces.c.end_time_unix_nano, stmt.excluded.end_time_unix_nano),
                }))


def _trace_values(owner_user_id: int, trace_id: str, start, end) -> dict:
    return {
        "owner_user_id": owner_user_id, "trace_id": trace_id,
        "start_time_unix_nano": start, "end_time_unix_nano": end,
    }
