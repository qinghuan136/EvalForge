"""内置助手的登录用户 API。"""

from fastapi import APIRouter, Depends, Path, Request, Response

from ..assistant.streaming import AssistantEventResponse, accepts_event_stream
from ..dependencies import Authenticated, current_user, require_csrf
from ..schemas.assistant import (
    AssistantChatRequest, AssistantChatResponse, AssistantSessionCreate, AssistantSessionDetail,
    AssistantSessionList, AssistantSessionSummary,
    AssistantStreamRequest,
)
from ..schemas.runners import RunnerCreateResponse
from ..schemas.common import MAX_DB_ID
from ..services.assistant import AssistantService

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


def assistant_service(request: Request) -> AssistantService:
    return request.app.state.assistant


@router.get("/sessions", response_model=AssistantSessionList)
async def list_sessions(identity: Authenticated = Depends(current_user),
                        service: AssistantService = Depends(assistant_service)):
    return await service.list_sessions(identity.user.id)


@router.post("/sessions", status_code=201, response_model=AssistantSessionSummary, dependencies=[Depends(require_csrf)])
async def create_session(data: AssistantSessionCreate, identity: Authenticated = Depends(current_user),
                         service: AssistantService = Depends(assistant_service)):
    return await service.create(identity.user.id, data.title)


@router.get("/sessions/{session_id}", response_model=AssistantSessionDetail)
async def get_session(session_id: int = Path(ge=1, le=MAX_DB_ID), identity: Authenticated = Depends(current_user),
                      service: AssistantService = Depends(assistant_service)):
    return await service.get(identity.user.id, session_id)


@router.post("/sessions/{session_id}/chat", response_model=AssistantChatResponse, dependencies=[Depends(require_csrf)],
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def chat(data: AssistantChatRequest, request: Request, response: Response,
               session_id: int = Path(ge=1, le=MAX_DB_ID),
               identity: Authenticated = Depends(current_user), service: AssistantService = Depends(assistant_service)):
    if accepts_event_stream(", ".join(request.headers.getlist("accept"))):
        return AssistantEventResponse(service, identity.user.id, session_id, data, identity.session.auth_version)
    response.headers["Vary"] = "Accept"
    return await service.chat(identity.user.id, session_id, data, identity.session.auth_version)


@router.post("/stream", dependencies=[Depends(require_csrf)],
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def stream(data: AssistantStreamRequest, identity: Authenticated = Depends(current_user),
                 service: AssistantService = Depends(assistant_service)):
    return AssistantEventResponse(service, identity.user.id, data.session_id, data, identity.session.auth_version)


@router.post("/sessions/{session_id}/runner-credentials/{tool_call_id}", response_model=RunnerCreateResponse,
             dependencies=[Depends(require_csrf)])
async def take_runner_credential(session_id: int = Path(ge=1, le=MAX_DB_ID),
                                 tool_call_id: str = Path(min_length=1, max_length=256),
                                 identity: Authenticated = Depends(current_user),
                                 service: AssistantService = Depends(assistant_service)):
    await service.get(identity.user.id, session_id)
    return service.conversations.take_credential(identity.user.id, session_id, tool_call_id)
