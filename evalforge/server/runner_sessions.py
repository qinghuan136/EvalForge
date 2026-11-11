"""单连接会话管理：一个事件消费者维护状态，租约不绑定 Job 的生命周期。"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError
from redis.exceptions import RedisError

from .config import ServerSettings
from .errors import APIError
from .experiment_events import ExperimentEventStore, PreviewTarget
from .rpc import JsonRpcPeer, RpcError
from .runner_auth import RunnerAuth, RunnerIdentity
from .runner_connections import RegisteredConnection, RunnerConnections
from .session_state_machine import (
    SessionContext, SessionEvent, SessionState, TransitionRejected,
    next_session_state, preview_session_transition, validate_session_event,
)
from .ws_transport import TransportClosed
from .schemas.attempts import AttemptKey, AttemptResult, RunnerReady, RunnerSync
from .schemas.output_preview import OutputPreviewBatch
from .services.attempts import AttemptError, AttemptService

logger = logging.getLogger(__name__)
T = TypeVar("T")


class RunnerHello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    runner_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(max_length=32)
    max_concurrent_jobs: StrictInt = Field(ge=1, le=1)

    @field_validator("runner_id", "version")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must not be blank")
        return value

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not v.strip() or len(v) > 64 for v in values):
            raise ValueError("Invalid capabilities")
        return values


class SessionRegistration(RegisteredConnection):
    cloud_instance_id: str
    owner_user_id: int
    credential_id: int
    client_version: str
    max_concurrent_jobs: int
    connected_at: datetime


@dataclass(frozen=True)
class HelloReceived:
    params: RunnerHello
    reply: asyncio.Future[dict]


@dataclass(frozen=True)
class AttemptReceived:
    method: str
    params: AttemptKey | AttemptResult | RunnerSync | RunnerReady
    reply: asyncio.Future[dict]


class RunnerSession:
    def __init__(self, manager: "RunnerSessionManager", identity: RunnerIdentity,
                 peer: JsonRpcPeer, settings: ServerSettings):
        self.manager, self.identity, self.peer, self.settings = manager, identity, peer, settings
        self.session_id = uuid4().hex
        self._phase = SessionState.CONNECTED
        self.connected_at = datetime.now(timezone.utc)
        self.events: asyncio.Queue[HelloReceived | AttemptReceived] = asyncio.Queue(settings.runner_session_queue_size)
        self.sync_processed = False
        self.pending_uploads: set[tuple[str, str]] = set()
        self.task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stop: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._registration_attempted = False
        self._registration: SessionRegistration | None = None
        self._preview_target: PreviewTarget | None = None
        self._publishing_preview = False

    @property
    def phase(self) -> SessionState:
        return self._phase

    @property
    def can_dispatch(self) -> bool:
        return (self.phase == SessionState.READY and not self._stop.done()
                and self._registration is not None and "http_json" in self._registration.capabilities)

    @property
    def supports_streaming(self) -> bool:
        return self._registration is not None and "http_sse" in self._registration.capabilities

    async def _transition(self, event: SessionEvent, context: SessionContext) -> None:
        """唯一阶段提交入口；先由状态机判断，存储确认后才更新本地阶段。"""
        preview = preview_session_transition(self.phase, event, context)
        if preview.reason is not None:
            raise TransitionRejected(self.phase, event, preview.reason)
        target = preview.next_state
        assert target is not None
        if target != SessionState.CLOSED:
            if self._stop.done():
                raise APIError(403, "runner_session_closed", "Runner 会话已关闭")
            if target != self.phase:
                if self._registration is None:
                    raise RuntimeError("Session registration is not confirmed")
                record = self._registration.model_copy(update={"phase": target.value})
                changed = await self.storage(self.manager.connections.compare_and_set_phase(
                    self.identity.runner_id, self.session_id, self.phase.value, record))
                if not changed:
                    raise APIError(403, "runner_session_replaced", "Runner 会话阶段已变化或失效")
                self._registration = record
            # 等待存储期间可能收到停止信号，不能再确认本地转换成功。
            if self._stop.done():
                raise APIError(403, "runner_session_closed", "Runner 会话已关闭")
        self._phase = target

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("Session already started")
        self.task = asyncio.create_task(self._run(), name="runner-session-" + self.session_id)

    def request_close(self, code: int = 1000) -> None:
        # 只投递停止信号；phase 的修改仍由唯一管理协程负责。
        if not self._stop.done():
            self._stop.set_result(code)
            self._preview_target = None

    @property
    def close_code(self) -> int:
        return self._stop.result() if self._stop.done() else 1000

    async def storage(self, operation: Awaitable[T]) -> T:
        async with asyncio.timeout(self.settings.runner_session_operation_timeout_seconds):
            return await operation

    async def check_current(self) -> None:
        if self._stop.done() or self.phase == SessionState.CLOSED:
            if self.close_code == 1011:
                raise APIError(503, "runner_session_unavailable", "Runner 会话暂时不可用")
            raise APIError(403, "runner_session_closed", "Runner 会话已关闭")
        if self._registration is not None:
            current = await self.storage(self.manager.connections.is_current(self.identity.runner_id, self.session_id))
            if not current:
                raise APIError(403, "runner_session_replaced", "Runner 会话已失效")

    async def hello(self, **params) -> dict:
        try:
            hello = RunnerHello.model_validate(params)
        except ValidationError:
            raise RpcError(-32602, "Invalid hello params") from None
        if hello.runner_id != self.identity.runner_id:
            raise RpcError(-32602, "Runner identity mismatch")
        return await self._submit(HelloReceived(hello, asyncio.get_running_loop().create_future()))

    async def _submit(self, event: HelloReceived | AttemptReceived) -> dict:
        if self.task is None or self.task.done() or self._stop.done():
            raise RpcError(-32001, "Session closed")
        reply = event.reply
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            raise RpcError(-32000, "Session capacity exceeded") from None
        try:
            return await asyncio.shield(reply)
        finally:
            if not reply.done():
                reply.cancel()
            elif not reply.cancelled():
                reply.exception()

    async def _attempt_request(self, method: str, model: type[BaseModel], params: dict) -> dict:
        try:
            parsed = model.model_validate(params)
        except (ValidationError, ValueError):
            raise RpcError(-32602, "Invalid attempt params") from None
        return await self._submit(AttemptReceived(method, parsed, asyncio.get_running_loop().create_future()))

    async def sync(self, **params) -> dict:
        return await self._attempt_request("runner.sync", RunnerSync, params)

    async def ready(self, **params) -> dict:
        return await self._attempt_request("runner.ready", RunnerReady, params)

    async def started(self, **params) -> dict:
        return await self._attempt_request("jobs.started", AttemptKey, params)

    async def complete(self, **params) -> dict:
        return await self._attempt_request("jobs.complete", AttemptResult, params)

    def set_preview_target(self, target: PreviewTarget | None) -> None:
        self._preview_target = target if self.can_dispatch and self.supports_streaming else None

    def clear_preview_target(self, key: AttemptKey) -> None:
        if self._preview_target is not None and self._preview_target.key == (key.job_id, key.attempt_id):
            self._preview_target = None

    async def output_chunk(self, **params) -> None:
        """预览不进入业务队列，也不等待 started 或查询当前会话。"""
        try:
            batch = OutputPreviewBatch.model_validate(params)
        except ValidationError:
            raise RpcError(-32602, "Invalid output preview params") from None
        target, store = self._preview_target, self.manager.event_store
        if (target is None or target.key != (batch.job_id, batch.attempt_id)
                or store is None or not self.can_dispatch or self._publishing_preview):
            return
        # 一个会话最多有一批在写 Redis。慢写时丢预览，不占满 RPC handler 等待席位。
        self._publishing_preview = True
        try:
            await self.storage(store.append_preview(target, batch.chunks))
        except (RedisError, TimeoutError) as exc:
            logger.debug("Preview unavailable session_id=%r error_type=%s", self.session_id, type(exc).__name__)
        finally:
            self._publishing_preview = False

    async def _handle_attempt(self, event: AttemptReceived) -> dict:
        """RPC 业务入口：单消费者串行修改同步事实，数据库异常在这里收口。"""
        await self.check_current()
        params = event.params
        if event.method == "runner.ready":
            if params.session_id != self.session_id:
                raise RpcError(-32001, "Session identity mismatch")
            try:
                await self._transition(SessionEvent.READY_REQUESTED, SessionContext(
                    is_current=True, sync_processed=self.sync_processed,
                    pending_uploads_count=len(self.pending_uploads)))
            except TransitionRejected:
                raise RpcError(-32002, "Session is not ready") from None
            self.manager.notify_work()
            return {"session_id": self.session_id, "status": "ready"}
        try:
            if self.phase not in {SessionState.SYNCING, SessionState.READY}:
                raise RpcError(-32001, "Hello required")
            service = self.manager.attempts
            if service is None:
                raise RpcError(-32001, "Attempt service unavailable")
            async with asyncio.timeout(self.settings.runner_session_operation_timeout_seconds):
                if event.method == "runner.sync":
                    if self.sync_processed or self.phase != SessionState.SYNCING:
                        raise RpcError(-32001, "Sync already processed")
                    result = await service.sync(self.identity, params)
                    await self.check_current()
                    self.pending_uploads = {(r.job_id, r.attempt_id) for r in params.pending_results}
                    self.sync_processed = True
                    return result
                if not self.sync_processed:
                    raise RpcError(-32001, "Sync required")
                if event.method == "jobs.started":
                    await service.started(self.identity, params)
                else:
                    await service.complete(self.identity, params)
                    self.clear_preview_target(params)
                    self.manager.notify_work()
                await self.check_current()
                if event.method == "jobs.complete":
                    self.pending_uploads.discard((params.job_id, params.attempt_id))
                return {"acknowledged": True}
        except AttemptError as exc:
            raise RpcError(-32002, str(exc)) from None
        except (RpcError, APIError, RedisError):
            raise
        except Exception as exc:
            # 不输出异常文本/SQL 参数，避免把凭据或 Agent 输入输出写进日志。
            logger.error("Attempt persistence failed method=%s runner_id=%r job_id=%r attempt_id=%r error_type=%s",
                         event.method, self.identity.runner_id, getattr(params, "job_id", None),
                         getattr(params, "attempt_id", None), type(exc).__name__)
            if isinstance(exc, (SQLAlchemyError, TimeoutError)):
                raise RpcError(-32003, "Attempt persistence unavailable") from None
            raise RpcError(-32603, "Internal error") from None

    async def _handle_hello(self, event: HelloReceived) -> dict:
        try:
            validate_session_event(self.phase, SessionEvent.HELLO_CONFIRMED)
        except TransitionRejected:
            raise RpcError(-32001, "Hello already completed") from None
        record = SessionRegistration(
            session_id=self.session_id, phase=self.phase.value, cloud_instance_id=self.manager.instance_id,
            owner_user_id=self.identity.owner_user_id, credential_id=self.identity.credential_id,
            client_version=event.params.version, capabilities=event.params.capabilities,
            max_concurrent_jobs=event.params.max_concurrent_jobs, connected_at=self.connected_at,
        )
        self._registration_attempted = True
        previous = await self.storage(self.manager.connections.register(
            self.identity.runner_id, record, self.settings.runner_session_ttl_seconds))
        self._registration = record
        # 使用 Redis 原子替换返回的旧 ID，避免并发 hello 误关闭更新的连接。
        if previous is not None and (old := self.manager.sessions.get(previous)) is not None:
            old.request_close(1008)
        await self.check_current()
        await self._transition(SessionEvent.HELLO_CONFIRMED, SessionContext(is_current=True, hello_validated=True))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="runner-heartbeat-" + self.session_id)
        logger.info("Runner hello accepted runner_id=%r session_id=%r", self.identity.runner_id, self.session_id)
        # 兼容现有客户端：此字段仅表示 hello 通过，真实阶段仍为 syncing。
        return {"session_id": self.session_id, "status": "ready"}

    def _seconds_until_expiry(self) -> float:
        return max(0, (self.identity.expires_at - datetime.now(timezone.utc)).total_seconds())

    async def _heartbeat_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.settings.runner_session_heartbeat_seconds
        next_check = loop.time() + interval
        try:
            while not self._stop.done():
                delay = min(max(0, next_check - loop.time()), self._seconds_until_expiry())
                await asyncio.wait({self._stop}, timeout=delay)
                if self._stop.done():
                    return
                if self._seconds_until_expiry() == 0:
                    self.request_close(1008)
                    return
                next_check = loop.time() + interval
                async with asyncio.timeout(self.settings.runner_session_operation_timeout_seconds):
                    await self.manager.runner_auth.check(self.identity)
                    if self._stop.done():
                        return
                    renewed = await self.manager.connections.renew(
                        self.identity.runner_id, self.session_id, self.settings.runner_session_ttl_seconds)
                if self._stop.done():
                    return
                # 续租只确认当前阶段仍有效，阶段写入仍归主协程。
                next_session_state(self.phase, SessionEvent.LEASE_RENEWED, SessionContext(is_current=renewed))
        except APIError as exc:
            self.request_close(1011 if exc.status >= 500 else 1008)
        except TransitionRejected:
            self.request_close(1008)
        except Exception as exc:
            logger.warning("Runner heartbeat unavailable session_id=%r error_type=%s",
                           self.session_id, type(exc).__name__)
            self.request_close(1011)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        hello_deadline = loop.time() + min(self.settings.runner_hello_timeout_seconds, self._seconds_until_expiry())
        event: HelloReceived | AttemptReceived | None = None
        getter: asyncio.Task | None = None
        try:
            while not self._stop.done():
                timeout = None
                if self.phase == SessionState.CONNECTED:
                    timeout = hello_deadline - loop.time()
                    if timeout <= 0:
                        self.request_close(1008)
                        break
                getter = asyncio.create_task(self.events.get())
                done, _ = await asyncio.wait({getter, self._stop}, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                else:
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                getter = None
                if self._stop.done():
                    break
                if event is None:
                    continue
                if not event.reply.done():
                    try:
                        async with asyncio.timeout_at(hello_deadline if self.phase == SessionState.CONNECTED else None):
                            result = (await self._handle_hello(event) if isinstance(event, HelloReceived)
                                      else await self._handle_attempt(event))
                    except TimeoutError:
                        if isinstance(event, HelloReceived) and loop.time() >= hello_deadline:
                            self.request_close(1008)
                            break
                        raise
                    except RpcError as exc:
                        if not event.reply.done():
                            event.reply.set_exception(exc.with_traceback(None))
                    else:
                        if not self._stop.done() and not event.reply.done():
                            event.reply.set_result(result)
                if self._stop.done():
                    break
                event = None
        except asyncio.CancelledError:
            raise
        except APIError as exc:
            self.request_close(1011 if exc.status >= 500 else 1008)
        except TransitionRejected:
            self.request_close(1008)
        except Exception as exc:
            logger.warning("Runner session unavailable session_id=%r error_type=%s", self.session_id, type(exc).__name__)
            self.request_close(1011)
        finally:
            self.request_close()
            await self._transition(SessionEvent.CLOSE, SessionContext())
            if getter is not None:
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
                if not getter.cancelled() and getter.exception() is None:
                    event = getter.result()
            if event is not None and not event.reply.done():
                event.reply.set_exception(TransportClosed("Runner session closed"))
            while not self.events.empty():
                pending = self.events.get_nowait()
                if not pending.reply.done():
                    pending.reply.set_exception(TransportClosed("Runner session closed"))
            if self._heartbeat_task is not None:
                await self._heartbeat_task
            try:
                if self._registration_attempted:
                    await self.storage(self.manager.connections.compare_delete(self.identity.runner_id, self.session_id))
            except Exception as exc:
                # 存储不可用时由 TTL 兜底，不把清理失败变成永久持有的本地对象。
                logger.warning("Runner session cleanup unavailable session_id=%r error_type=%s", self.session_id, type(exc).__name__)
            finally:
                self.manager.sessions.pop(self.session_id, None)
                logger.info("Runner session closed runner_id=%r session_id=%r", self.identity.runner_id, self.session_id)

    async def close(self, code: int = 1000) -> None:
        self.request_close(code)
        if self.task is not None:
            await asyncio.shield(self.task)


class RunnerSessionManager:
    def __init__(self, connections: RunnerConnections, attempts: AttemptService | None = None, *,
                 runner_auth: RunnerAuth, event_store: ExperimentEventStore | None = None):
        self.connections = connections
        self.attempts = attempts
        self.runner_auth = runner_auth
        self.event_store = event_store
        self.on_work_available: Callable[[], None] | None = None
        self.instance_id = uuid4().hex
        self.sessions: dict[str, RunnerSession] = {}
        self._closing = False

    def notify_work(self) -> None:
        if self.on_work_available is not None:
            self.on_work_available()

    def ready_sessions(self) -> list[RunnerSession]:
        return [session for session in self.sessions.values() if session.can_dispatch]

    def open(self, identity: RunnerIdentity, peer: JsonRpcPeer, settings: ServerSettings) -> RunnerSession:
        if self._closing:
            raise TransportClosed("Server shutting down")
        session = RunnerSession(self, identity, peer, settings)
        self.sessions[session.session_id] = session
        session.start()
        return session

    async def close(self) -> None:
        self._closing = True
        sessions = list(self.sessions.values())
        for session in sessions:
            session.request_close(1001)
        await asyncio.gather(*(session.close(1001) for session in sessions))
        # 唤醒网络读取者；路由负责等待 RPC 清理完成。
        await asyncio.gather(*(session.peer.close() for session in sessions), return_exceptions=True)
