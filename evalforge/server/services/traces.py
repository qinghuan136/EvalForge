"""按所属用户查询 Trace 列表、调用树和单个 Span。"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from evalforge.db import Database
from evalforge.db.models import Span, Trace

from ..errors import APIError
from ..schemas.traces import SpanDetail, SpanSummary, TraceDetail, TracePage, TraceSummary

_SPAN_FIELDS = (
    "span_id", "parent_span_id", "name", "kind", "status_code",
    "start_time_unix_nano", "end_time_unix_nano",
)
_SPAN_COLUMNS = tuple(getattr(Span, key) for key in _SPAN_FIELDS)
_SPAN_ORDER = (Span.start_time_unix_nano, Span.id)
_TRACE_COLUMNS = (Trace.id, Trace.trace_id, Trace.start_time_unix_nano, Trace.end_time_unix_nano)
_RAW_FIELDS = (
    "status_message", "trace_state", "flags", "attributes", "events", "links",
    "resource", "instrumentation_scope", "dropped_attributes_count", "dropped_events_count",
    "dropped_links_count",
)


def _with_representative_span(query):
    traces = query.subquery()
    candidate = aliased(Span)
    first = (
        select(candidate.id)
        .where(candidate.trace_row_id == traces.c.id)
        .order_by(candidate.start_time_unix_nano, candidate.id)
        .limit(1).correlate(traces)
    )
    root = first.where(candidate.parent_span_id.is_(None)).scalar_subquery()
    representative_id = func.coalesce(root, first.scalar_subquery())
    return (
        select(traces, Span.span_id, Span.parent_span_id, Span.name, Span.status_code, Span.resource)
        .outerjoin(Span, Span.id == representative_id)
        .order_by(traces.c.start_time_unix_nano.desc(), traces.c.id.desc())
    )


def _times(row) -> dict[str, str]:
    # NUMERIC stores exact nanoseconds. JSON strings preserve the value in browsers.
    return {
        "start_time_unix_nano": str(int(row.start_time_unix_nano)),
        "end_time_unix_nano": str(int(row.end_time_unix_nano)),
    }


def _service_name(resource: dict | None) -> str | None:
    attributes = (resource or {}).get("resource", {}).get("attributes", [])
    for item in attributes:
        if item.get("key") == "service.name":
            value = item.get("value", {}).get("stringValue")
            return value if isinstance(value, str) and value.strip() else None
    return None


def _trace_summary(row) -> TraceSummary:
    root = row.span_id if row.parent_span_id is None else None
    return TraceSummary(
        trace_id=row.trace_id, title=row.name or "未命名调用链", service_name=_service_name(row.resource),
        root_span_id=root, status_code=row.status_code if root else None, **_times(row),
    )


def _span_fields(row) -> dict:
    return {**{key: getattr(row, key) for key in _SPAN_FIELDS}, **_times(row)}


class TraceQueryService:
    def __init__(self, database: Database):
        self.database = database

    async def list_traces(self, owner_id: int, page: int, pagesize: int, trace_id: str | None) -> TracePage:
        scope = [Trace.owner_user_id == owner_id]
        if trace_id is not None:
            scope.append(Trace.trace_id == trace_id)
        query = select(*_TRACE_COLUMNS).where(*scope).order_by(Trace.start_time_unix_nano.desc(), Trace.id.desc())
        async with self.database.transaction() as db:
            total = await db.scalar(select(func.count()).select_from(Trace).where(*scope))
            rows = await db.execute(_with_representative_span(query.limit(pagesize).offset((page - 1) * pagesize)))
            return TracePage(items=[_trace_summary(row) for row in rows], page=page, pagesize=pagesize, total=total)

    async def get_trace(self, owner_id: int, trace_id: str, after_span_id: str | None, limit: int) -> TraceDetail:
        async with self.database.transaction() as db:
            query = select(*_TRACE_COLUMNS).where(Trace.owner_user_id == owner_id, Trace.trace_id == trace_id)
            trace = (await db.execute(_with_representative_span(query))).one_or_none()
            if trace is None:
                raise APIError(404, "trace_not_found", "调用链不存在")
            scope = Span.trace_row_id == trace.id
            query = select(*_SPAN_COLUMNS).where(scope).order_by(*_SPAN_ORDER).limit(limit + 1)
            if after_span_id is not None:
                cursor = (await db.execute(select(*_SPAN_ORDER).where(scope, Span.span_id == after_span_id))).one_or_none()
                if cursor is None:
                    raise APIError(404, "span_not_found", "Span 不存在")
                start, row_id = cursor
                query = query.where(or_(Span.start_time_unix_nano > start,
                                        and_(Span.start_time_unix_nano == start, Span.id > row_id)))
            rows = (await db.execute(query)).all()
            return TraceDetail(
                **_trace_summary(trace).model_dump(),
                spans=[SpanSummary(**_span_fields(row)) for row in rows[:limit]],
                next_span_id=rows[limit - 1].span_id if len(rows) > limit else None,
            )

    async def get_span(self, owner_id: int, trace_id: str, span_id: str) -> SpanDetail:
        async with self.database.transaction() as db:
            span = await db.scalar(select(Span).join(Trace).where(
                Trace.owner_user_id == owner_id, Trace.trace_id == trace_id, Span.span_id == span_id,
            ))
            if span is None:
                raise APIError(404, "span_not_found", "Span 不存在")
            return SpanDetail(trace_id=trace_id, **_span_fields(span),
                              **{key: getattr(span, key) for key in _RAW_FIELDS})

