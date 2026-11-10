"""调度本地 Runner 的 Task，以及数据库中的待评分工作。"""

import asyncio
import logging
from datetime import datetime, timezone
from itertools import islice

from redis.exceptions import RedisError

from .errors import APIError
from .rpc import RpcError, RpcProtocolError, RpcRequestTooLarge
from .runner_sessions import RunnerSession, RunnerSessionManager
from .services.dispatch import CancelRequest, DispatchService, RETRYABLE_REJECTIONS
from .services.evaluations import EvaluationService, PendingEvaluation
from .ws_transport import TransportClosed

logger = logging.getLogger(__name__)
EVAL_SCAN_BATCH = 100


class Scheduler:
    def __init__(self, sessions: RunnerSessionManager, dispatch: DispatchService, *,
                 evaluations: EvaluationService | None = None, poll_interval: float = 1,
                 dispatch_timeout: float = 5, retry_delay: float = 5):
        self.sessions, self.dispatch = sessions, dispatch
        self.evaluations = evaluations
        self.poll_interval, self.dispatch_timeout, self.retry_delay = poll_interval, dispatch_timeout, retry_delay
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop: asyncio.Task | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._capacity = asyncio.Semaphore(32)
        self._retry_after: dict[str, float] = {}
        self._cancel_acks: dict[str, str] = {}  # session_id → 已确认接收取消指令的 attempt_id
        self._eval_candidates: set[int] = set()
        self._eval_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._recover_evals = evaluations is not None
        self._eval_cursor: tuple[int, str] | None = None

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run(), name="scheduler")

    def wake(self) -> None:
        self._wake.set()

    def notify_eval(self, run_id: int) -> None:
        if self.evaluations is not None and not self._stopping:
            self._eval_candidates.add(run_id)
            self.wake()

    async def close(self) -> None:
        self._stopping = True
        self.wake()
        if self._loop is not None:
            await self._loop
        evaluations = tuple(self._eval_tasks.values())
        for task in evaluations:
            task.cancel()
        # 让已经发出的派发在 RPC 超时内收尾，资源由 lifespan 在这之后关闭。
        await asyncio.gather(*self._tasks.values(), *evaluations, return_exceptions=True)
        self._eval_tasks.clear()

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            now = asyncio.get_running_loop().time()
            self._retry_after = {key: deadline for key, deadline in self._retry_after.items() if deadline > now}
            sessions = self.sessions.ready_sessions()
            session_ids = {session.session_id for session in sessions}
            self._cancel_acks = {key: value for key, value in self._cancel_acks.items() if key in session_ids}
            for session in sessions:
                runner_id = session.identity.runner_id
                if runner_id not in self._tasks and runner_id not in self._retry_after:
                    task = asyncio.create_task(self._dispatch_one(session), name=f"dispatch-{runner_id}")
                    self._tasks[runner_id] = task
            await self._schedule_evals()
            try:
                async with asyncio.timeout(self.poll_interval):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def _schedule_evals(self) -> None:
        if self.evaluations is None or self._stopping:
            return
        run_ids = list(islice(self._eval_candidates, EVAL_SCAN_BATCH))
        self._eval_candidates.difference_update(run_ids)
        try:
            if run_ids:
                await self._start_evals(run_ids)
            if self._recover_evals:
                items = await self._start_evals(after=self._eval_cursor, limit=EVAL_SCAN_BATCH)
                self._recover_evals = len(items) == EVAL_SCAN_BATCH
                if self._recover_evals:
                    self._eval_cursor = items[-1].key
        except Exception as exc:
            self._eval_candidates.update(run_ids)
            logger.error("Eval discovery failed error_type=%s", type(exc).__name__)
        else:
            if self._eval_candidates or self._recover_evals:
                self.wake()

    async def _start_evals(self, run_ids: list[int] | None = None, *,
                           after: tuple[int, str] | None = None, limit: int | None = None) -> list[PendingEvaluation]:
        assert self.evaluations is not None
        in_flight = set(self._eval_tasks)  # 查询返回前，部分协程可能已经保存并退出。
        items = await self.evaluations.find_missing(run_ids, after=after, limit=limit)
        if not self._stopping:
            for item in items:
                if item.key not in in_flight and item.key not in self._eval_tasks:
                    self._eval_tasks[item.key] = asyncio.create_task(self._run_eval(item), name=f"eval-{item.run_id}")
        return items

    async def _run_eval(self, item: PendingEvaluation) -> None:
        assert self.evaluations is not None
        try:
            await self.evaluations.run(item)
        except Exception as exc:
            logger.error("Eval work failed run_id=%s error_type=%s", item.run_id, type(exc).__name__)
        finally:
            self._eval_tasks.pop(item.key, None)

    async def _can_dispatch(self, session: RunnerSession) -> bool:
        if self._stopping or not session.can_dispatch:
            return False
        try:
            await session.check_current()
        except (APIError, RedisError, TimeoutError):
            return False
        return not self._stopping and session.can_dispatch

    async def _dispatch_one(self, session: RunnerSession) -> None:
        identity = session.identity
        try:
            async with self._capacity:
                if await self._can_dispatch(session):
                    await self._send_one(session)
        except Exception as exc:
            logger.error("Dispatch failed runner_id=%r error_type=%s", identity.runner_id, type(exc).__name__)
        finally:
            self._tasks.pop(identity.runner_id, None)

    async def _send_one(self, session: RunnerSession) -> None:
        identity = session.identity
        action = await self.dispatch.next_action(identity)
        if isinstance(action, CancelRequest):
            await self._send_cancel(session, action)
            return
        self._cancel_acks.pop(session.session_id, None)
        if action is None:
            return
        claim = action
        if not await self._can_dispatch(session):
            # 从未进入 RPC，因此可以安全释放这次预留。
            await self.dispatch.record(identity, claim, outcome="rejected", reason="session_unavailable")
            return
        if not await self.dispatch.prepare_send(identity, claim):
            return

        sent_at = datetime.now(timezone.utc)
        try:
            reply = await session.peer.call("jobs.dispatch", claim.payload, timeout=self.dispatch_timeout)
        except RpcRequestTooLarge:
            await self.dispatch.record(identity, claim, outcome="rejected", reason="dispatch_payload_too_large")
            return
        except TimeoutError:
            outcome, reason = "unknown", "dispatch_timeout"
        except (TransportClosed, RpcError, RpcProtocolError):
            outcome, reason = "unknown", "dispatch_outcome_unknown"
        except Exception as exc:
            logger.warning("RPC dispatch failed runner_id=%r error_type=%s", identity.runner_id, type(exc).__name__)
            outcome, reason = "unknown", "dispatch_outcome_unknown"
        else:
            accepted = reply.get("accepted") if isinstance(reply, dict) else None
            reason = reply.get("reason", "dispatch_rejected") if isinstance(reply, dict) else None
            if accepted is True and isinstance(reason, str):
                outcome, reason = "accepted", None
            elif accepted is False and isinstance(reason, str):
                outcome = "rejected"
            else:
                outcome, reason = "unknown", "invalid_dispatch_reply"
        try:
            changed = await self.dispatch.record(identity, claim, outcome=outcome, reason=reason, dispatched_at=sent_at)
        except Exception:
            if outcome == "unknown":
                session.request_close(1011)
            raise
        if changed and outcome == "unknown":
            # 触发重连对账，再给新会话派发后续 Job。
            session.request_close(1011)
        if changed and outcome == "rejected" and reason in RETRYABLE_REJECTIONS:
            self._retry_after[identity.runner_id] = asyncio.get_running_loop().time() + self.retry_delay
        if outcome == "accepted":
            self.wake()  # 接单期间写入的取消意图可以立即处理。

    async def _send_cancel(self, session: RunnerSession, request: CancelRequest) -> None:
        if self._cancel_acks.get(session.session_id) == request.key.attempt_id:
            return
        if not await self._can_dispatch(session):
            return
        try:
            reply = await session.peer.call("jobs.cancel", {**request.key.model_dump(), "reason": request.reason},
                                            timeout=self.dispatch_timeout)
        except (TimeoutError, TransportClosed, RpcError, RpcProtocolError):
            reply = None
        if isinstance(reply, dict):
            if reply.get("accepted") is True:
                self._cancel_acks[session.session_id] = request.key.attempt_id
                return  # 最终状态由 jobs.complete 写入。
            if reply.get("reason") == "attempt_not_found":
                session.request_close(1011)  # 重连快照可以区分缺失尝试和已有结果。
                return
        self._retry_after[session.identity.runner_id] = asyncio.get_running_loop().time() + self.retry_delay
