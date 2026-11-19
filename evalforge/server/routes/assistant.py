"""内置助手的登录用户 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response

from ..assistant.streaming import AssistantEventResponse, accepts_event_stream
from ..dependencies import Authenticated, current_user, require_csrf
from ..schemas.assistant import (
    AssistantChatRequest, AssistantChatResponse, AssistantDecisionRequest, AssistantSessionCreate, AssistantSessionDetail,
    AssistantSessionList, AssistantSessionSummary,
    AssistantStreamRequest, ConversationId,
)
from ..services.assistant import AssistantService

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])
routed_router = APIRouter(tags=["assistant"])


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
async def get_session(session_id: ConversationId, identity: Authenticated = Depends(current_user),
                      service: AssistantService = Depends(assistant_service)):
    return await service.get(identity.user.id, session_id)


@router.post("/sessions/{session_id}/chat", response_model=AssistantChatResponse, dependencies=[Depends(require_csrf)],
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def chat(data: AssistantChatRequest, request: Request, response: Response,
               session_id: ConversationId,
               identity: Authenticated = Depends(current_user), service: AssistantService = Depends(assistant_service)):
    if accepts_event_stream(", ".join(request.headers.getlist("accept"))):
        return AssistantEventResponse(service, identity.user.id, session_id, data, identity.session.auth_version)
    response.headers["Vary"] = "Accept"
    return await service.chat(identity.user.id, session_id, data, identity.session.auth_version)


@router.post("/stream", dependencies=[Depends(require_csrf)],
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def stream(data: AssistantStreamRequest, identity: Authenticated = Depends(current_user),
                 service: AssistantService = Depends(assistant_service)):
    return AssistantEventResponse(service, identity.user.id, data.conversation_id, data, identity.session.auth_version)


@routed_router.post("/api/v1/runners/{runner_id}/assistant/stream", dependencies=[Depends(require_csrf)],
                    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def routed_decision(data: AssistantDecisionRequest,
                          runner_id: Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$")],
                          identity: Authenticated = Depends(current_user),
                          service: AssistantService = Depends(assistant_service)):
    service.check_local_runner(runner_id, identity.user.id)
    return AssistantEventResponse(service, identity.user.id, data.conversation_id, data, identity.session.auth_version)
