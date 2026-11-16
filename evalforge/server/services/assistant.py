"""会话历史与一轮对话的编排。模型调用和等待续租都在短事务之外。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from anyio import CancelScope
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta, UserPromptPart,
)
from pydantic_ai.usage import UsageLimits
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import AssistantMessage, AssistantSession

from ..assistant.agent import build_agent, build_model
from ..assistant.context import (
    AssistantDeps, history_for_model, prompt_with_page_context, require_accessible_page_context, with_user_text,
)
from ..config import ServerSettings
from ..errors import APIError
from ..schemas.assistant import (
    AssistantChatRequest, AssistantChatResponse, AssistantMessageView, AssistantSessionDetail,
    AssistantSessionList, AssistantSessionSummary,
)
from ..schemas.common import validate_database_json
from ..schemas.page_context import ExperimentPageContext
from .experiments import ExperimentService

logger = logging.getLogger(__name__)
HISTORY_TURNS = 12
VISIBLE_MESSAGES = 100
Session = AssistantSession
Message = AssistantMessage
EmitText = Callable[[str], Awaitable[None]]


@dataclass
class AssistantTurn:
    user_id: int
    session_id: int
    data: AssistantChatRequest
    run_id: UUID
    model: str
    history: list[dict]
    finalized: bool = False


def _lease_clock(db: AsyncSession, seconds: int = 0):
    if db.get_bind().dialect.name == "sqlite":
        # SQLite is used for offline tests; compare values in the same datetime format.
        return func.datetime("now", f"+{seconds} seconds")
    now = func.clock_timestamp()
    return now + timedelta(seconds=seconds) if seconds else now


def _view(row: Message) -> AssistantMessageView:
    return AssistantMessageView(
        id=row.id, role=row.role, text=row.content["text"], model_provider=row.model_provider,
        model_name=row.model_name, source_experiment_id=row.content.get("source_experiment_id"),
        page_context=row.content.get("page_context"),
        created_at=row.created_at,
    )


def _lease_lost() -> APIError:
    return APIError(409, "assistant_run_expired", "本轮对话已失去执行权，请刷新后重试")


class AssistantService:
    def __init__(self, database: Database, experiments: ExperimentService, config: ServerSettings) -> None:
        self.database = database
        self.experiments = experiments
        self.config = config

    @property
    def models(self) -> tuple[str, ...]:
        return self.config.assistant_models if self.config.assistant_api_key else ()

    async def list_sessions(self, user_id: int) -> AssistantSessionList:
        async with self.database.transaction() as db:
            rows = (await db.scalars(select(Session).where(Session.user_id == user_id)
                                     .order_by(Session.updated_at.desc(), Session.id.desc()).limit(50))).all()
        return AssistantSessionList(items=[AssistantSessionSummary.model_validate(row) for row in rows],
                                    models=list(self.models))

    async def create(self, user_id: int, title: str) -> AssistantSessionSummary:
        async with self.database.transaction() as db:
            row = Session(user_id=user_id, title=title)
            db.add(row)
            await db.flush()
            summary = AssistantSessionSummary.model_validate(row)
        return summary

    async def get(self, user_id: int, session_id: int) -> AssistantSessionDetail:
        async with self.database.transaction() as db:
            # Hold a shared lock only while reading a consistent transcript/version pair.
            row = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id)
                                  .with_for_update(read=True))
            if row is None:
                raise APIError(404, "assistant_session_not_found", "助手会话不存在")
            messages = (await db.scalars(select(Message)
                        .where(Message.session_id == session_id, Message.role.in_(("user", "assistant")),
                               Message.content["text"].as_string().is_not(None))
                        .order_by(Message.id.desc()).limit(VISIBLE_MESSAGES))).all()
            return AssistantSessionDetail(**AssistantSessionSummary.model_validate(row).model_dump(),
                                          messages=[_view(m) for m in reversed(messages)])

    async def chat(self, user_id: int, session_id: int, data: AssistantChatRequest) -> AssistantChatResponse:
        async with self.prepare_chat(user_id, session_id, data) as turn:
            return await self.execute_chat(turn)

    @asynccontextmanager
    async def prepare_chat(self, user_id: int, session_id: int, data: AssistantChatRequest) -> AsyncIterator[AssistantTurn]:
        """Hold the lease for the response lifetime, including SSE disconnects."""
        if not self.models:
            raise APIError(503, "assistant_model_unconfigured", "管理员尚未配置助手模型")
        model = data.model_name or self.models[0]
        if model not in self.models:
            raise APIError(422, "assistant_model_unsupported", "此模型未在助手的可用模型列表中")
        run_id = uuid4()
        turn = AssistantTurn(user_id, session_id, data, run_id, model, await self._claim(user_id, session_id, run_id, data))
        try:
            yield turn
        finally:
            await self._release_chat(turn)

    async def _release_chat(self, turn: AssistantTurn) -> None:
        if turn.finalized:
            return
        with CancelScope(shield=True):
            try:
                async with self.database.transaction() as db:
                    await db.execute(self._owned_update(db, turn.user_id, turn.session_id, turn.run_id)
                                     .values(active_run_id=None, lease_expires_at=None))
            except SQLAlchemyError:
                logger.warning("Assistant lease release failed session_id=%d", turn.session_id)
            turn.finalized = True

    async def execute_chat(self, turn: AssistantTurn, emit: EmitText | None = None) -> AssistantChatResponse:
        try:
            async with asyncio.timeout(self.config.assistant_timeout_seconds):
                rows = await self._run_with_heartbeat(turn, emit)
            result = await self._save(turn.user_id, turn.session_id, turn.run_id, rows)
            turn.finalized = True
            return result
        except TimeoutError:
            raise APIError(504, "assistant_timeout", "助手回答超时，请重试") from None
        finally:
            await self._release_chat(turn)

    async def _claim(self, user_id: int, session_id: int, run_id: UUID, data: AssistantChatRequest) -> list[dict]:
        async with self.database.transaction() as db:
            scope = (Session.id == session_id, Session.user_id == user_id)
            claimed = await db.scalar(
                update(Session).where(*scope, Session.version == data.expected_version,
                                      or_(Session.active_run_id.is_(None),
                                          Session.lease_expires_at <= _lease_clock(db)))
                .values(active_run_id=run_id, lease_expires_at=_lease_clock(db, self.config.assistant_lease_seconds))
                .returning(Session.id)
            )
            if claimed is None:
                version = await db.scalar(select(Session.version).where(*scope))
                if version is None:
                    raise APIError(404, "assistant_session_not_found", "助手会话不存在")
                if version != data.expected_version:
                    raise APIError(409, "assistant_history_changed", "会话历史已变化，请刷新后重试")
                raise APIError(409, "assistant_session_busy", "此会话已有一轮对话正在执行")
            await require_accessible_page_context(db, user_id, data.page_context)
            recent_users = (
                select(Message.id).where(Message.session_id == session_id, Message.role == "user")
                .order_by(Message.id.desc()).limit(HISTORY_TURNS).subquery()
            )
            # Cut at a user message so a model tool call never loses its matching tool result.
            stored = (await db.scalars(select(Message.content)
                       .where(Message.session_id == session_id, Message.status == "completed",
                              Message.id >= select(func.min(recent_users.c.id)).scalar_subquery())
                       .order_by(Message.id))).all()
            return list(stored)

    @staticmethod
    def _owned_update(db: AsyncSession, user_id: int, session_id: int, run_id: UUID):
        return update(Session).where(Session.id == session_id, Session.user_id == user_id,
                                     Session.active_run_id == run_id, Session.lease_expires_at > _lease_clock(db))

    async def _heartbeat(self, user_id: int, session_id: int, run_id: UUID) -> None:
        while True:
            await asyncio.sleep(self.config.assistant_heartbeat_seconds)
            async with self.database.transaction() as db:
                renewed = await db.scalar(self._owned_update(db, user_id, session_id, run_id)
                                          .values(lease_expires_at=_lease_clock(db, self.config.assistant_lease_seconds))
                                          .returning(Session.id))
                if renewed is None:
                    raise _lease_lost()

    async def _run_with_heartbeat(self, turn: AssistantTurn, emit: EmitText | None) -> list[Message]:
        heartbeat = asyncio.create_task(self._heartbeat(turn.user_id, turn.session_id, turn.run_id))
        execution = asyncio.create_task(self._run(turn, emit))
        try:
            done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat  # Propagate lease loss or database failure.
            return await execution
        finally:
            for task in (heartbeat, execution):
                if not task.done():
                    task.cancel()
            await asyncio.gather(heartbeat, execution, return_exceptions=True)

    async def _run(self, turn: AssistantTurn, emit: EmitText | None) -> list[Message]:
        data, model_name = turn.data, turn.model
        context = data.page_context
        experiment = context if isinstance(context, ExperimentPageContext) else None
        deps = AssistantDeps(turn.user_id, context, self.experiments)
        page_context = context.model_dump(mode="json", exclude_none=True) if context is not None else None

        async def publish_text(_, events):
            assert emit is not None
            async for event in events:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                    await emit(event.part.content)
                elif (isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta)
                      and event.delta.content_delta):
                    await emit(event.delta.content_delta)

        try:
            model = build_model(model_name, self.config.assistant_api_key, self.config.assistant_base_url)
            result = await build_agent(model, has_experiment_context=experiment is not None).run(
                prompt_with_page_context(data.message, page_context), deps=deps, message_history=history_for_model(turn.history),
                usage_limits=UsageLimits(request_limit=6, tool_calls_limit=8), model_settings={"max_tokens": 1500},
                event_stream_handler=publish_text if emit is not None else None,
            )
            # Keep original user text in history; rebuild the model-only block from metadata on replay.
            messages = [
                with_user_text(message, data.message)
                if isinstance(message, ModelRequest) and any(isinstance(part, UserPromptPart) for part in message.parts)
                else message
                for message in result.new_messages()
            ]
            payload = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
            rows = []
            for index, (message, encoded) in enumerate(zip(messages, payload, strict=True)):
                content = {"v": 1, "message": encoded}
                if isinstance(message, ModelResponse):
                    if index == len(messages) - 1:
                        content["text"] = result.output
                        if deps.progress_read and experiment is not None:
                            content["source_experiment_id"] = experiment.id
                    row = Message(session_id=turn.session_id, role="assistant", content=content,
                                  model_provider=message.provider_name or "openai",
                                  model_name=message.model_name or model_name)
                else:
                    assert isinstance(message, ModelRequest)
                    if any(isinstance(part, UserPromptPart) for part in message.parts):
                        content["text"] = data.message
                        if page_context is not None:
                            content["page_context"] = page_context
                    row = Message(session_id=turn.session_id, role="user" if "text" in content else "tool", content=content)
                validate_database_json(content)
                rows.append(row)
            return rows
        except (APIError, SQLAlchemyError, TimeoutError):
            raise
        except Exception as exc:
            logger.warning("Assistant model failed session_id=%d error_type=%s", turn.session_id, type(exc).__name__)
            raise APIError(502, "assistant_model_failed", "助手模型调用失败，请稍后重试") from None

    async def _save(self, user_id: int, session_id: int, run_id: UUID,
                    rows: list[Message]) -> AssistantChatResponse:
        async with self.database.transaction() as db:
            version = await db.scalar(
                self._owned_update(db, user_id, session_id, run_id)
                .values(version=Session.version + 1, updated_at=func.now(), active_run_id=None, lease_expires_at=None)
                .returning(Session.version)
            )
            if version is None:
                raise _lease_lost()
            db.add_all(rows)
            await db.flush()
            response = AssistantChatResponse(version=version, messages=[_view(row) for row in rows if "text" in row.content])
        return response
