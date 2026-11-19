"""Database-backed conversations and short-lived execution leases."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from anyio import CancelScope
from pydantic import JsonValue
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.conversation import Conversation
from pydantic_ai.conversation_store import ConversationConflict, ConversationEntry
from pydantic_ai.messages import (DeferredToolRequestsEvent, FunctionToolCallEvent, FunctionToolResultEvent,
                                 ModelRequest, ModelResponse, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta, UserPromptPart)
from pydantic_ai.usage import UsageLimits
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from evalforge.db import Database
from evalforge.db.models import AssistantConversationRecord as Record
from evalforge.db.models import AssistantMessage, AssistantSession

from ..assistant.agent import build_agent, build_model
from ..assistant.conversation_store import DatabaseConversationStore
from ..assistant.context import AssistantDeps, prompt_with_page_context, require_accessible_page_context
from ..assistant.presentation import display_conversation, display_deferred, display_tool_part
from ..config import ServerSettings
from ..errors import APIError
from ..schemas.assistant import (AssistantAction, AssistantChatRequest, AssistantChatResponse, AssistantDecisionRequest,
                                 AssistantDisplayCard, AssistantMessageView, AssistantSessionDetail, AssistantSessionList, AssistantSessionSummary,
                                 AssistantToolCallEvent, AssistantToolDeferredEvent, AssistantToolEvent)
from .datasets import DatasetService
from .experiment_configs import ExperimentConfigService
from .experiments import ExperimentService
from .runners import RunnerService
from .traces import TraceQueryService

logger = logging.getLogger(__name__)
EmitText = Callable[[str], Awaitable[None]]
EmitDisplay = Callable[[AssistantDisplayCard], Awaitable[None]]
EmitEvent = Callable[[str, dict[str, JsonValue]], Awaitable[None]]


def _lease_clock(db, seconds: int = 0):
    if db.get_bind().dialect.name == "sqlite":
        return func.datetime("now", f"+{seconds} seconds")
    now = func.clock_timestamp()
    return now + timedelta(seconds=seconds) if seconds else now


def _actions(conversation: Conversation) -> list[AssistantAction]:
    requests = conversation.deferred_tool_requests
    cards = []
    labels = {"RUNNER_create_runner": "创建 Runner", "RUNNER_delete_runner": "删除 Runner 并撤销凭证",
              "DATASET_import_dataset": "导入数据集", "EXPERIMENT_create_experiment": "启动实验",
              "EXPERIMENT_start_run": "启动实验 Run", "EXPERIMENT_cancel_experiment": "取消整场实验"}
    for call in requests.approvals if requests else []:
        arguments = dict(call.args_as_dict())
        description = arguments.pop("approval_description", None)
        target = (arguments.get("name") or arguments.get("config_id") or arguments.get("runner_id")
                  or arguments.get("experiment_id") or "")
        cards.append(AssistantAction(tool_call_id=call.tool_call_id, tool=call.tool_name, arguments=arguments,
            description=description or f"{labels.get(call.tool_name, call.tool_name)} {target}".strip()))
    return cards


def _view(entry: ConversationEntry) -> AssistantMessageView | None:
    message = entry.message
    metadata = (message.metadata or {}).get("evalforge", {})
    if "text" not in metadata:
        return None
    return AssistantMessageView(
        id=entry.id, role="user" if isinstance(message, ModelRequest) else "assistant", text=metadata["text"],
        model_provider=message.provider_name if isinstance(message, ModelResponse) else None,
        model_name=message.model_name if isinstance(message, ModelResponse) else None,
        source_experiment_id=metadata.get("source_experiment_id"), page_context=metadata.get("page_context"),
        created_at=message.timestamp or next(part.timestamp for part in message.parts if isinstance(part, UserPromptPart)),
    )


def _lease_lost():
    return APIError(409, "assistant_run_expired", "本轮对话已失去执行权，请刷新后重试")


@dataclass
class AssistantTurn:
    user_id: int
    session_id: str
    data: AssistantChatRequest | AssistantDecisionRequest
    run_id: UUID
    model: str
    conversation: Conversation
    auth_version: int
    finalized: bool = False


class AssistantService:
    def __init__(self, database: Database, experiments: ExperimentService, config: ServerSettings, *,
                 runners: RunnerService, datasets: DatasetService, traces: TraceQueryService,
                 experiment_configs: ExperimentConfigService | None = None,
                 has_ready_runner: Callable[[str, int], bool] | None = None):
        self.database, self.experiments, self.config = database, experiments, config
        self.runners, self.datasets, self.traces = runners, datasets, traces
        self.experiment_configs = experiment_configs or ExperimentConfigService(database)
        self.has_ready_runner = has_ready_runner

    def check_local_runner(self, runner_id: str, user_id: int) -> None:
        if self.has_ready_runner is not None and not self.has_ready_runner(runner_id, user_id):
            raise APIError(503, "runner_not_ready_here", "当前实例没有就绪的 Runner 会话，请稍后重试")

    def store(self, user_id: int, *, session=None):
        return DatabaseConversationStore(self.database, user_id, session=session)

    @property
    def models(self):
        return self.config.assistant_models if self.config.assistant_api_key else ()

    async def list_sessions(self, user_id: int) -> AssistantSessionList:
        await self._import_legacy(user_id)
        async with self.database.transaction() as db:
            rows = await db.scalars(select(Record).where(Record.user_id == user_id)
                                    .order_by(Record.updated_at.desc(), Record.conversation_id.desc()).limit(50))
            return AssistantSessionList(items=[AssistantSessionSummary.model_validate(row) for row in rows], models=list(self.models))

    async def _import_legacy(self, user_id: int):
        """Keep old SQL histories readable. Their lost in-memory approval state cannot be resumed."""
        async with self.database.transaction() as db:
            legacy = (await db.scalars(select(AssistantSession).where(AssistantSession.user_id == user_id))).all()
            existing = set(await db.scalars(select(Record.conversation_id).where(Record.user_id == user_id)))
        for session in legacy:
            conversation_id = f"legacy-{session.id}"
            if conversation_id in existing:
                continue
            try:
                async with self.database.transaction() as db:
                    contents = list(await db.scalars(select(AssistantMessage.content).where(
                        AssistantMessage.session_id == session.id).order_by(AssistantMessage.id)))
                    messages = ModelMessagesTypeAdapter.validate_python([content["message"] for content in contents])
                    for message, content in zip(messages, contents, strict=True):
                        if "text" in content:
                            metadata = {key: content[key] for key in ("text", "page_context", "source_experiment_id") if key in content}
                            message.metadata = {**(message.metadata or {}), "evalforge": metadata}
                    conversation = Conversation(conversation_id=conversation_id, messages=messages)
                    store = self.store(user_id, session=db)
                    await store.save(conversation, scope=store.scope)
                    await db.execute(update(Record).where(Record.conversation_id == conversation_id,
                        Record.user_id == user_id).values(title=session.title, created_at=session.created_at,
                        updated_at=session.updated_at, status="failed", last_error={
                            "code": "assistant_legacy_read_only", "message": "旧会话历史已保留，请新建会话继续。"}))
            except ConversationConflict:
                pass  # Another instance imported the same legacy history.

    async def create(self, user_id: int, title: str) -> AssistantSessionSummary:
        conversation = Conversation()
        async with self.database.transaction() as db:
            store = self.store(user_id, session=db)
            await store.save(conversation, scope=store.scope)
            row = await db.scalar(update(Record).where(Record.conversation_id == conversation.conversation_id,
                                                       Record.user_id == user_id).values(title=title).returning(Record))
            return AssistantSessionSummary.model_validate(row)

    async def _recover_expired(self, db, user_id: int, conversation_id: str):
        # A crashed approval run may already have executed a tool. Persist that uncertainty before any takeover.
        expired = (Record.conversation_id == conversation_id, Record.user_id == user_id,
                   Record.status == "running", Record.lease_expires_at <= _lease_clock(db))
        for approval in (True, False):
            code = "assistant_execution_uncertain" if approval else "assistant_run_expired"
            await db.execute(update(Record).where(*expired, Record.deferred_tool_requests.is_not(None) if approval
                             else Record.deferred_tool_requests.is_(None)).values(
                status="failed", active_run_id=None, lease_expires_at=None,
                last_error={"code": code, "message": "执行已中断，请先核对业务状态。" if approval else "上轮执行已中断，可以重新提问。"}))

    async def get(self, user_id: int, session_id: str) -> AssistantSessionDetail:
        if session_id.isdecimal() or session_id.startswith("legacy-"):
            await self._import_legacy(user_id)
            session_id = f"legacy-{session_id}" if session_id.isdecimal() else session_id
        async with self.database.transaction() as db:
            await self._recover_expired(db, user_id, session_id)
        # Retry a read when a concurrent completed run changed the revision between queries.
        for _ in range(3):
            async with self.database.transaction() as db:
                row = await db.scalar(select(Record).where(Record.conversation_id == session_id, Record.user_id == user_id))
                if row is None:
                    raise APIError(404, "assistant_session_not_found", "助手会话不存在")
                summary = AssistantSessionSummary.model_validate(row)
                status, error = row.status, row.last_error
            store = self.store(user_id)
            conversation = await store.load(session_id, scope=store.scope)
            entries = await store.load_entries(session_id, scope=store.scope)
            current = await store.read_record(session_id)
            if conversation is not None and current is not None and summary.revision == conversation.revision == current.revision:
                messages = [view for entry in entries or [] if (view := _view(entry)) is not None][-100:]
                return AssistantSessionDetail(**summary.model_dump(), status=status, error=error, messages=messages,
                    conversation=display_conversation(conversation),
                    actions=_actions(conversation) if status == "awaiting_approval" else [])
        raise APIError(409, "assistant_history_changed", "会话历史已变化，请刷新后重试")

    @staticmethod
    def _owned_update(db, user_id: int, session_id: str, run_id: UUID):
        return update(Record).where(Record.conversation_id == session_id, Record.user_id == user_id,
                                    Record.active_run_id == run_id, Record.lease_expires_at > _lease_clock(db))

    @asynccontextmanager
    async def prepare_chat(self, user_id: int, session_id: str, data: AssistantChatRequest | AssistantDecisionRequest,
                           auth_version: int) -> AsyncIterator[AssistantTurn]:
        if not self.models:
            raise APIError(503, "assistant_model_unconfigured", "管理员尚未配置助手模型")
        run_id = uuid4()
        decision = isinstance(data, AssistantDecisionRequest)
        async with self.database.transaction() as db:
            await self._recover_expired(db, user_id, session_id)
        async with self.database.transaction() as db:
            row = await db.scalar(select(Record).where(Record.conversation_id == session_id, Record.user_id == user_id))
            if row is None:
                raise APIError(404, "assistant_session_not_found", "助手会话不存在")
            if row.revision != data.expected_revision:
                raise APIError(409, "assistant_history_changed", "会话历史已变化，请刷新后重试")
            if row.status == "running":
                raise APIError(409, "assistant_session_busy", "此会话已有一轮对话正在执行")
            if row.last_error and row.last_error["code"] in {"assistant_execution_uncertain", "assistant_legacy_read_only"}:
                raise APIError(409, row.last_error["code"], "请查询业务状态后新建会话")
            store = self.store(user_id, session=db)
            conversation = await store.load(session_id, scope=store.scope)
            actions = _actions(conversation)
            if decision:
                if row.status != "awaiting_approval" or not actions:
                    raise APIError(409, "assistant_no_pending_approval", "当前没有可恢复的审批")
                if set(data.decisions) != {action.tool_call_id for action in actions}:
                    raise APIError(422, "assistant_invalid_decisions", "必须一次提交全部待审批调用的决定")
                for action in actions:
                    if action.tool == "EXPERIMENT_start_run" and data.decisions[action.tool_call_id]:
                        runner_id = action.arguments.get("runner_id")
                        if isinstance(runner_id, str):
                            self.check_local_runner(runner_id, user_id)
            elif actions:
                raise APIError(409, "assistant_approval_required", "请先处理当前待审批操作")
            model = row.resume_model_name if decision else data.model_name or self.models[0]
            if model not in self.models:
                raise APIError(422, "assistant_model_unsupported", "此模型未在助手的可用模型列表中")
            await require_accessible_page_context(db, user_id, getattr(data, "page_context", None))
            claimed = await db.scalar(update(Record).where(
                Record.conversation_id == session_id, Record.user_id == user_id, Record.revision == data.expected_revision,
                Record.status != "running",
            ).values(status="running", active_run_id=run_id,
                     lease_expires_at=_lease_clock(db, self.config.assistant_lease_seconds),
                     resume_model_name=model, last_error=None).returning(Record.conversation_id))
            if claimed is None:
                raise APIError(409, "assistant_session_busy", "会话已由其他请求领取，请刷新")
        turn = AssistantTurn(user_id, session_id, data, run_id, model, conversation, auth_version)
        try:
            yield turn
        finally:
            await self._release_chat(turn)

    async def _release_chat(self, turn: AssistantTurn):
        if turn.finalized:
            return
        code = "assistant_execution_uncertain" if isinstance(turn.data, AssistantDecisionRequest) else "assistant_run_failed"
        with CancelScope(shield=True):
            try:
                async with self.database.transaction() as db:
                    await db.execute(update(Record).where(Record.conversation_id == turn.session_id,
                        Record.user_id == turn.user_id, Record.active_run_id == turn.run_id).values(
                        status="failed", active_run_id=None, lease_expires_at=None,
                        last_error={"code": code, "message": "本次执行未完成，请核对业务状态。"}))
            except SQLAlchemyError:
                logger.warning("Assistant lease release failed conversation_id=%s", turn.session_id)
            turn.finalized = True

    async def chat(self, user_id, session_id, data, auth_version):
        async with self.prepare_chat(user_id, session_id, data, auth_version) as turn:
            return await self.execute_chat(turn)

    async def _heartbeat(self, turn):
        while True:
            await asyncio.sleep(self.config.assistant_heartbeat_seconds)
            async with self.database.transaction() as db:
                renewed = await db.scalar(self._owned_update(db, turn.user_id, turn.session_id, turn.run_id)
                    .values(lease_expires_at=_lease_clock(db, self.config.assistant_lease_seconds)).returning(Record.conversation_id))
                if renewed is None:
                    raise _lease_lost()

    async def execute_chat(self, turn: AssistantTurn, emit: EmitText | None = None,
                           emit_display: EmitDisplay | None = None, emit_event: EmitEvent | None = None):
        heartbeat = asyncio.create_task(self._heartbeat(turn))
        execution = asyncio.create_task(self._run(turn, emit, emit_display, emit_event))
        try:
            async with asyncio.timeout(self.config.assistant_timeout_seconds):
                done, _ = await asyncio.wait((heartbeat, execution), return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    await heartbeat
                result = await execution
            response = await self._save(turn, result)
            turn.finalized = True
            return response
        except TimeoutError:
            raise APIError(504, "assistant_timeout", "助手回答超时，请重试") from None
        except ConversationConflict:
            raise APIError(409, "assistant_history_changed", "会话历史已变化，请刷新") from None
        finally:
            for task in (heartbeat, execution):
                task.cancel()
            await asyncio.gather(heartbeat, execution, return_exceptions=True)
            await self._release_chat(turn)

    async def _run(self, turn: AssistantTurn, emit: EmitText | None, emit_display: EmitDisplay | None,
                   emit_event: EmitEvent | None):
        data = turn.data
        decision = isinstance(data, AssistantDecisionRequest)
        if decision and emit_display is None:
            raise APIError(409, "assistant_stream_required", "请通过 SSE 提交审批决定")
        context = getattr(data, "page_context", None)
        page_context = context.model_dump(mode="json", exclude_none=True) if context else None

        async def check_execution():
            async with self.database.transaction() as db:
                checked = await db.scalar(self._owned_update(db, turn.user_id, turn.session_id, turn.run_id)
                                          .values(active_run_id=turn.run_id).returning(Record.conversation_id))
                if checked is None:
                    raise _lease_lost()

        async def display(card: AssistantDisplayCard):
            if emit_display is None:
                raise APIError(409, "assistant_stream_required", "请通过 SSE 接收展示卡片")
            await emit_display(card)

        async def publish_events(_, events):
            identity = {"conversation_id": turn.session_id, "run_id": str(turn.run_id)}
            async for event in events:
                if emit and isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                    await emit(event.part.content)
                elif emit and isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    await emit(event.delta.content_delta)
                elif emit_event:
                    if isinstance(event, FunctionToolCallEvent):
                        payload = AssistantToolCallEvent(**identity, part=display_tool_part(event.part), args_valid=event.args_valid)
                        await emit_event("tool_call", payload.model_dump(mode="json"))
                    elif isinstance(event, FunctionToolResultEvent):
                        payload = AssistantToolEvent(**identity, part=display_tool_part(event.part))
                        await emit_event("tool_result", payload.model_dump(mode="json"))
                    elif isinstance(event, DeferredToolRequestsEvent):
                        payload = AssistantToolDeferredEvent(**identity, requests=display_deferred(event.requests))
                        await emit_event("tool_deferred", payload.model_dump(mode="json"))

        deps = AssistantDeps(turn.user_id, self.experiments, self.runners, self.datasets, self.traces,
                             turn.auth_version, check_execution, display, self.experiment_configs,
                             self.check_local_runner)
        conversation = turn.conversation
        requests = conversation.deferred_tool_requests
        deferred = requests.build_results(approvals=data.decisions) if decision else None
        try:
            model = build_model(turn.model, self.config.assistant_api_key, self.config.assistant_base_url)
            result = await build_agent(model).run(
                None if decision else prompt_with_page_context(data.message, page_context),
                deps=deps, conversation=conversation, deferred_tool_results=deferred, run_id=str(turn.run_id),
                usage_limits=UsageLimits(request_limit=conversation.usage.requests + 6,
                                         tool_calls_limit=conversation.usage.tool_calls + 8),
                model_settings={"max_tokens": 1500}, event_stream_handler=publish_events if emit or emit_event else None)
            new = result.new_messages()
            for index, message in enumerate(new):
                metadata = {}
                if not decision and isinstance(message, ModelRequest) and any(isinstance(part, UserPromptPart) for part in message.parts):
                    metadata = {"text": data.message, "page_context": page_context}
                elif index == len(new) - 1 and isinstance(message, ModelResponse) and isinstance(result.output, str):
                    metadata = {"text": result.output, "source_experiment_id": deps.source_experiment_id}
                if metadata:
                    message.metadata = {**(message.metadata or {}), "evalforge": metadata}
            return result
        except (APIError, SQLAlchemyError, TimeoutError):
            raise
        except Exception as exc:
            logger.warning("Assistant model failed conversation_id=%s error_type=%s", turn.session_id, type(exc).__name__)
            raise APIError(502, "assistant_model_failed", "助手模型调用失败，请稍后重试") from None

    async def _save(self, turn, result):
        status = "awaiting_approval" if result.conversation.deferred_tool_requests else "completed"
        async with self.database.transaction() as db:
            owned = self._owned_update(db, turn.user_id, turn.session_id, turn.run_id)
            if await db.scalar(owned.values(active_run_id=turn.run_id).returning(Record.conversation_id)) is None:
                raise _lease_lost()
            store = self.store(turn.user_id, session=db)
            entries = await store.save(result, scope=store.scope)
            saved = await db.scalar(owned.values(status=status, active_run_id=None, lease_expires_at=None,
                                                 last_error=None).returning(Record.conversation_id))
            if saved is None:
                raise _lease_lost()
            response = AssistantChatResponse(revision=result.conversation.revision, status=status,
                messages=[view for entry in entries if (view := _view(entry)) is not None], actions=_actions(result.conversation))
        return response
