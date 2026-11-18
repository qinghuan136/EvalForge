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
from pydantic_ai.conversation import Conversation, ConversationTypeAdapter
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
from ..assistant.conversations import FakeConversationStore, StoredConversation
from ..assistant.context import AssistantDeps, prompt_with_page_context, require_accessible_page_context, with_user_text
from ..config import ServerSettings
from ..errors import APIError
from ..schemas.assistant import (
    AssistantChatRequest, AssistantChatResponse, AssistantDecisionRequest, AssistantMessageView, AssistantSessionDetail,
    AssistantSessionList, AssistantSessionSummary,
)
from ..schemas.common import validate_database_json
from .datasets import DatasetService
from .experiments import ExperimentService
from .runners import RunnerService
from .traces import TraceQueryService

logger = logging.getLogger(__name__)
VISIBLE_MESSAGES = 100
Session = AssistantSession
Message = AssistantMessage
EmitText = Callable[[str], Awaitable[None]]


@dataclass
class AssistantTurn:
    user_id: int
    session_id: int
    data: AssistantChatRequest | AssistantDecisionRequest
    run_id: UUID
    model: str
    stored: StoredConversation
    auth_version: int
    finalized: bool = False
    conversation: Conversation | None = None


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
    def __init__(self, database: Database, experiments: ExperimentService, config: ServerSettings, *,
                 runners: RunnerService, datasets: DatasetService, traces: TraceQueryService) -> None:
        self.database = database
        self.experiments = experiments
        self.config = config
        self.runners, self.datasets, self.traces = runners, datasets, traces
        self.conversations = FakeConversationStore()

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
            stored = await self.conversations.load_conversation(user_id, session_id)
            status = stored.status if stored else ("completed" if row.version == 0 else "unavailable")
            error = stored.error if stored else None
            if stored and stored.version != row.version and status != "running" and (
                not error or error["code"] != "assistant_execution_uncertain"
            ):
                status = "unavailable"
            if status == "unavailable":
                error = {"code": "assistant_conversation_missing", "message": "内存对话现场已丢失，请新建会话"}
            return AssistantSessionDetail(**AssistantSessionSummary.model_validate(row).model_dump(),
                messages=[_view(m) for m in reversed(messages)], status=status,
                actions=stored.actions() if stored and status == "awaiting_approval" else [], error=error,
                credential_call_ids=self.conversations.credential_ids(user_id, session_id))

    async def chat(self, user_id: int, session_id: int, data: AssistantChatRequest,
                   auth_version: int) -> AssistantChatResponse:
        async with self.prepare_chat(user_id, session_id, data, auth_version) as turn:
            return await self.execute_chat(turn)

    @asynccontextmanager
    async def prepare_chat(self, user_id: int, session_id: int,
                           data: AssistantChatRequest | AssistantDecisionRequest,
                           auth_version: int) -> AsyncIterator[AssistantTurn]:
        """Hold the lease for the response lifetime, including SSE disconnects."""
        if not self.models:
            raise APIError(503, "assistant_model_unconfigured", "管理员尚未配置助手模型")
        async with self.database.transaction() as db:
            if await db.scalar(select(Session.id).where(Session.id == session_id, Session.user_id == user_id)) is None:
                raise APIError(404, "assistant_session_not_found", "助手会话不存在")
        lock = self.conversations.lock(session_id)
        if lock.locked():
            raise APIError(409, "assistant_session_busy", "此会话已有一轮对话正在执行")
        await lock.acquire()
        try:
            run_id = uuid4()
            stored = await self.conversations.load_conversation(user_id, session_id)
            decision = isinstance(data, AssistantDecisionRequest)
            model = stored.model if decision and stored else getattr(data, "model_name", None) or self.models[0]
            if model not in self.models:
                raise APIError(422, "assistant_model_unsupported", "此模型未在助手的可用模型列表中")
            # Checks run inside the claim transaction so rejected requests do not leave a lease.
            await self._claim(user_id, session_id, run_id, data, stored)
            stored = stored or StoredConversation(user_id, data.expected_version,
                ConversationTypeAdapter.dump_python(Conversation(), mode="json"), model)
            turn = AssistantTurn(user_id, session_id, data, run_id, model, stored, auth_version)
            active = StoredConversation(user_id, stored.version, stored.snapshot, model,
                stored.page_context, "running")
            try:
                await self.conversations.save_conversation(session_id, active)
                yield turn
            finally:
                await self._release_chat(turn)
        finally:
            lock.release()

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
            # An interrupted approved run may already have performed a write. Never replay it automatically.
            restored = turn.stored
            restored.status = "failed"
            restored.error = {"code": "assistant_execution_uncertain" if isinstance(turn.data, AssistantDecisionRequest)
                              else "assistant_run_failed",
                              "message": "本次执行未完成，请查询业务状态后新建会话" if isinstance(turn.data, AssistantDecisionRequest)
                              else "本轮未完成，请查询会话状态后继续"}
            self.conversations.records[turn.session_id] = restored
            turn.finalized = True

    async def execute_chat(self, turn: AssistantTurn, emit: EmitText | None = None) -> AssistantChatResponse:
        try:
            async with asyncio.timeout(self.config.assistant_timeout_seconds):
                rows = await self._run_with_heartbeat(turn, emit)
            result = await self._save(turn.user_id, turn.session_id, turn.run_id, rows)
            assert turn.conversation is not None
            state = StoredConversation(turn.user_id, result.version,
                ConversationTypeAdapter.dump_python(turn.conversation, mode="json"), turn.model,
                getattr(turn.data, "page_context", None).model_dump(mode="json", exclude_none=True)
                if getattr(turn.data, "page_context", None) is not None else turn.stored.page_context,
                "awaiting_approval" if turn.conversation.deferred_tool_requests else "completed")
            await self.conversations.save_conversation(turn.session_id, state)
            turn.finalized = True
            result.actions = state.actions()
            result.status = state.status
            return result
        except TimeoutError:
            raise APIError(504, "assistant_timeout", "助手回答超时，请重试") from None
        finally:
            await self._release_chat(turn)

    async def _claim(self, user_id: int, session_id: int, run_id: UUID,
                     data: AssistantChatRequest | AssistantDecisionRequest,
                     stored: StoredConversation | None) -> None:
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
            await require_accessible_page_context(db, user_id, getattr(data, "page_context", None))
            if (stored is None and data.expected_version != 0) or (stored and stored.version != data.expected_version):
                raise APIError(409, "assistant_conversation_missing", "内存对话现场已丢失或版本不一致，请新建会话")
            if stored and stored.error and stored.error["code"] == "assistant_execution_uncertain":
                raise APIError(409, "assistant_execution_uncertain", "上次执行结果不确定，请查询业务状态后新建会话")
            actions = stored.actions() if stored else []
            if isinstance(data, AssistantDecisionRequest):
                if not stored or stored.status != "awaiting_approval" or not actions:
                    raise APIError(409, "assistant_no_pending_approval", "当前没有可恢复的审批")
                if set(data.decisions) != {action.tool_call_id for action in actions}:
                    raise APIError(422, "assistant_invalid_decisions", "必须一次提交全部待审批调用的决定")
            elif actions:
                raise APIError(409, "assistant_approval_required", "请先处理当前待审批操作")

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
        decision = isinstance(data, AssistantDecisionRequest)
        context = getattr(data, "page_context", None)
        async def check_execution():
            async with self.database.transaction() as db:
                if await db.scalar(self._owned_update(db, turn.user_id, turn.session_id, turn.run_id)
                                   .values(active_run_id=turn.run_id)
                                   .returning(Session.id)) is None:
                    raise _lease_lost()

        def deliver(call_id, response):
            self.conversations.credentials[(turn.session_id, call_id)] = (turn.user_id, response)

        deps = AssistantDeps(turn.user_id, self.experiments, self.runners, self.datasets, self.traces,
                             turn.auth_version, check_execution, deliver)
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
            conversation = turn.stored.restore()
            requests = conversation.deferred_tool_requests
            deferred = requests.build_results(approvals=data.decisions) if decision and requests else None
            # Conversation usage is cumulative; apply the budget to this run's additional work.
            result = await build_agent(model).run(
                None if decision else prompt_with_page_context(data.message, page_context),
                deps=deps, conversation=conversation, deferred_tool_results=deferred,
                usage_limits=UsageLimits(request_limit=conversation.usage.requests + 6,
                                         tool_calls_limit=conversation.usage.tool_calls + 8),
                model_settings={"max_tokens": 1500},
                event_stream_handler=publish_text if emit is not None else None,
            )
            # SQL rows retain original user text; the fake snapshot keeps the model-facing context.
            messages = [
                with_user_text(message, data.message)
                if not decision and isinstance(message, ModelRequest) and any(isinstance(part, UserPromptPart) for part in message.parts)
                else message
                for message in result.new_messages()
            ]
            payload = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
            rows = []
            for index, (message, encoded) in enumerate(zip(messages, payload, strict=True)):
                content = {"v": 1, "message": encoded}
                if isinstance(message, ModelResponse):
                    if index == len(messages) - 1 and isinstance(result.output, str):
                        content["text"] = result.output
                        if deps.source_experiment_id is not None:
                            content["source_experiment_id"] = deps.source_experiment_id
                    row = Message(session_id=turn.session_id, role="assistant", content=content,
                                  model_provider=message.provider_name or "openai",
                                  model_name=message.model_name or model_name)
                else:
                    assert isinstance(message, ModelRequest)
                    if not decision and any(isinstance(part, UserPromptPart) for part in message.parts):
                        content["text"] = data.message
                        if page_context is not None:
                            content["page_context"] = page_context
                    row = Message(session_id=turn.session_id, role="user" if "text" in content else "tool", content=content)
                validate_database_json(content)
                rows.append(row)
            turn.conversation = result.conversation
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
