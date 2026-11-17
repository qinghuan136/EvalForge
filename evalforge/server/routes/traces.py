"""浏览器登录用户的 Trace 查询 API；上报仍走独立的 OTLP 路由。"""

from fastapi import APIRouter, Depends, Path, Query, Request

from ..dependencies import Authenticated, current_user
from ..schemas.common import MAX_DB_ID
from ..schemas.traces import SpanDetail, TraceDetail, TracePage
from ..services.traces import TraceQueryService

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])
_TRACE_ID = r"^[0-9a-f]{32}$"
_SPAN_ID = r"^[0-9a-f]{16}$"


def trace_service(request: Request) -> TraceQueryService:
    return request.app.state.trace_queries


@router.get("", response_model=TracePage)
async def list_traces(page: int = Query(1, ge=1, le=MAX_DB_ID),
                      pagesize: int = Query(20, ge=1, le=100),
                      trace_id: str | None = Query(None, pattern=_TRACE_ID),
                      identity: Authenticated = Depends(current_user),
                      service: TraceQueryService = Depends(trace_service)) -> TracePage:
    return await service.list_traces(identity.user.id, page, pagesize, trace_id)


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(trace_id: str = Path(pattern=_TRACE_ID),
                    after_span_id: str | None = Query(None, pattern=_SPAN_ID),
                    limit: int = Query(250, ge=1, le=500),
                    identity: Authenticated = Depends(current_user),
                    service: TraceQueryService = Depends(trace_service)) -> TraceDetail:
    return await service.get_trace(identity.user.id, trace_id, after_span_id, limit)


@router.get("/{trace_id}/spans/{span_id}", response_model=SpanDetail)
async def get_span(trace_id: str = Path(pattern=_TRACE_ID),
                   span_id: str = Path(pattern=_SPAN_ID),
                   identity: Authenticated = Depends(current_user),
                   service: TraceQueryService = Depends(trace_service)) -> SpanDetail:
    return await service.get_span(identity.user.id, trace_id, span_id)
