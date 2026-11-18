"""SSE transport for a single assistant turn."""

import asyncio
import logging
from collections.abc import AsyncIterator

from anyio import CancelScope
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Receive, Scope, Send

from ..errors import APIError
from ..experiment_events import event_frame
from ..schemas.assistant import AssistantChatRequest, AssistantDecisionRequest
from ..services.assistant import AssistantService, AssistantTurn

logger = logging.getLogger(__name__)
KEEP_ALIVE_SECONDS = 15
EVENT_BUFFER_SIZE = 32


def accepts_event_stream(accept: str) -> bool:
    for entry in accept.split(","):
        media_type, *parameters = entry.split(";")
        if media_type.strip().lower() != "text/event-stream":
            continue
        quality = next((value.strip() for param in parameters for key, _, value in (param.partition("="),)
                        if key.strip().lower() == "q"), "1")
        try:
            if 0 < float(quality) <= 1:
                return True
        except ValueError:
            continue
    return False


class AssistantEventResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, service: AssistantService, user_id: int, session_id: int,
                 data: AssistantChatRequest | AssistantDecisionRequest, auth_version: int):
        super().__init__()
        self.service, self.user_id, self.session_id, self.data = service, user_id, session_id, data
        self.auth_version = auth_version

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Reject conflicts and ownership errors before sending HTTP response headers.
        async with self.service.prepare_chat(self.user_id, self.session_id, self.data, self.auth_version) as turn:
            events = stream_events(self.service, turn)
            response = StreamingResponse(events, media_type=self.media_type, headers={
                "Vary": "Accept", "Cache-Control": "no-store", "X-Accel-Buffering": "no",
            })
            try:
                await response(scope, receive, send)
            finally:
                # A disconnect can interrupt send() while the iterator is suspended after yield.
                with CancelScope(shield=True):
                    await events.aclose()


async def stream_events(service: AssistantService, turn: AssistantTurn) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=EVENT_BUFFER_SIZE)

    async def emit(text: str) -> None:
        await queue.put(("text_delta", {"text": text}))

    async def run() -> None:
        try:
            result = await service.execute_chat(turn, emit)
        except APIError as exc:
            await queue.put(("error", {"code": exc.code, "message": exc.message}))
        except SQLAlchemyError as exc:
            logger.warning("Assistant stream storage failed session_id=%d error_type=%s",
                           turn.session_id, type(exc).__name__)
            await queue.put(("error", {"code": "service_unavailable", "message": "助手存储暂时不可用"}))
        except Exception as exc:
            logger.error("Assistant stream failed session_id=%d error_type=%s", turn.session_id, type(exc).__name__)
            await queue.put(("error", {"code": "internal_error", "message": "助手暂时无法完成对话"}))
        else:
            if result.actions:
                await queue.put(("action_required", {"version": result.version,
                    "actions": [action.model_dump(mode="json") for action in result.actions]}))
            await queue.put(("done", result.model_dump(mode="json")))

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                async with asyncio.timeout(KEEP_ALIVE_SECONDS):
                    name, data = await queue.get()
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield event_frame(name, data)
            if name in {"done", "error"}:
                return
    finally:
        task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(task, return_exceptions=True)
