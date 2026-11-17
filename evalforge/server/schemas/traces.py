"""浏览器读取 Trace 的轻量摘要和 Span 原始详情。"""

from pydantic import JsonValue

from .auth import Schema


class TraceSummary(Schema):
    trace_id: str
    title: str
    service_name: str | None
    root_span_id: str | None
    status_code: int | None
    start_time_unix_nano: str
    end_time_unix_nano: str


class TracePage(Schema):
    items: list[TraceSummary]
    page: int
    pagesize: int
    total: int


class SpanSummary(Schema):
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    status_code: int | None
    start_time_unix_nano: str
    end_time_unix_nano: str


class TraceDetail(TraceSummary):
    spans: list[SpanSummary]
    next_span_id: str | None


class SpanDetail(SpanSummary):
    trace_id: str
    status_message: str | None
    trace_state: str
    flags: int
    attributes: list[dict[str, JsonValue]]
    events: list[dict[str, JsonValue]]
    links: list[dict[str, JsonValue]]
    resource: dict[str, JsonValue] | None
    instrumentation_scope: dict[str, JsonValue] | None
    dropped_attributes_count: int
    dropped_events_count: int
    dropped_links_count: int
