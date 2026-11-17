"""Decode an OTLP/HTTP protobuf batch without interpreting span attributes."""

import gzip
import io
import zlib
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

PROTOBUF = "application/x-protobuf"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_SPANS = 4096
SpanValues = dict[str, Any]


class OtlpRequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _span_id(value: bytes, size: int, field: str) -> str:
    if len(value) != size or not any(value):
        raise OtlpRequestError(400, f"Invalid {field}")
    return value.hex()


def decode_spans(body: bytes | bytearray, encoding: str) -> list[SpanValues]:
    if encoding == "gzip":
        if not body:
            raise OtlpRequestError(400, "Invalid gzip request body")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                body = stream.read(MAX_BODY_BYTES + 1)
        except (OSError, EOFError, zlib.error):
            raise OtlpRequestError(400, "Invalid gzip request body") from None
        if len(body) > MAX_BODY_BYTES:
            raise OtlpRequestError(413, "Decompressed request body is too large")
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except (DecodeError, UnicodeError):
        raise OtlpRequestError(400, "Invalid ExportTraceServiceRequest") from None

    spans: dict[tuple[str, str], SpanValues] = {}
    count = 0
    for resource_spans in request.resource_spans:
        resource = _metadata(resource_spans, "resource")
        for scope_spans in resource_spans.scope_spans:
            if not scope_spans.spans:
                continue
            count += len(scope_spans.spans)
            if count > MAX_SPANS:
                raise OtlpRequestError(413, "Too many spans in one request")
            scope = _metadata(scope_spans, "scope")
            for span in scope_spans.spans:
                row = _span_values(span, resource, scope)
                spans.setdefault((row["trace_id"], row["span_id"]), row)
    return list(spans.values())


def _metadata(group, field: str) -> dict | None:
    result = {}
    if group.HasField(field):
        result[field] = MessageToDict(getattr(group, field), use_integers_for_enums=True)
    if group.schema_url:
        result["schemaUrl"] = group.schema_url
    return result or None


def _span_values(span, resource: dict | None, scope: dict | None) -> SpanValues:
    has_status = span.HasField("status")
    if span.end_time_unix_nano < span.start_time_unix_nano:
        raise OtlpRequestError(400, "Span end time precedes start time")
    if not span.name or any("\0" in value for value in (span.name, span.trace_state, span.status.message)):
        raise OtlpRequestError(400, "Invalid span name, trace state or status message")
    trace_id = _span_id(span.trace_id, 16, "trace ID")
    span_id = _span_id(span.span_id, 8, "span ID")
    parent_id = _span_id(span.parent_span_id, 8, "parent span ID") if span.parent_span_id else None
    payload = MessageToDict(span, use_integers_for_enums=True)
    links = payload.get("links", [])
    # OTLP JSON uses hexadecimal IDs; generic Protobuf JSON encodes bytes in base64.
    for raw, link in zip(span.links, links):
        if raw.trace_id:
            link["traceId"] = raw.trace_id.hex()
        if raw.span_id:
            link["spanId"] = raw.span_id.hex()
    return {
        "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent_id,
        "name": span.name, "kind": span.kind,
        "start_time_unix_nano": span.start_time_unix_nano, "end_time_unix_nano": span.end_time_unix_nano,
        "status_code": span.status.code if has_status else None,
        "status_message": span.status.message if has_status else None,
        "trace_state": span.trace_state, "flags": span.flags,
        "attributes": payload.get("attributes", []), "events": payload.get("events", []), "links": links,
        "resource": resource, "instrumentation_scope": scope,
        "dropped_attributes_count": span.dropped_attributes_count,
        "dropped_events_count": span.dropped_events_count, "dropped_links_count": span.dropped_links_count,
    }
