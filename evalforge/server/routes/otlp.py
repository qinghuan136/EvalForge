"""OTLP/HTTP entry point; reports success only after the ingest transaction commits."""

import logging

from fastapi import APIRouter, Request, Response
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from ..errors import APIError
from ..otlp import MAX_BODY_BYTES, PROTOBUF, OtlpRequestError, decode_spans

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/v1/traces", response_class=Response)
async def export_traces(request: Request) -> Response:
    try:
        headers = request.headers.getlist("authorization")
        identity = await request.app.state.runner_auth.verify(headers[0] if len(headers) == 1 else None)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != PROTOBUF:
            raise OtlpRequestError(415, f"Expected {PROTOBUF}")
        encoding = request.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip"}:
            raise OtlpRequestError(415, "Unsupported Content-Encoding")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > MAX_BODY_BYTES - len(body):
                raise OtlpRequestError(413, "Request body is too large")
            body.extend(chunk)
        rows = await run_in_threadpool(decode_spans, body, encoding)
        await request.app.state.trace_ingest.ingest(identity.owner_user_id, rows)
    except APIError as exc:
        message = "Invalid Runner credential" if exc.status == 401 else "Runner or account is disabled"
        return _status(exc.status, message)
    except OtlpRequestError as exc:
        return _status(exc.status, str(exc))
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("OTLP trace ingest unavailable error_type=%s", type(exc).__name__)
        return _status(503, "Trace service unavailable")
    return Response(ExportTraceServiceResponse().SerializeToString(), media_type=PROTOBUF)


def _status(code: int, message: str) -> Response:
    headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
    return Response(Status(message=message).SerializeToString(), status_code=code,
                    headers=headers, media_type=PROTOBUF)
