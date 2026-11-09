"""每个云端进程只调度自身持有的会话。"""

import asyncio
import logging
from datetime import datetime, timezone

from redis.exceptions import RedisError

from .errors import APIError
from .rpc import RpcError, RpcProtocolError, RpcRequestTooLarge
from .runner_sessions import RunnerSession, RunnerSessionManager
from .services.dispatch import DispatchService, RETRYABLE_REJECTIONS
from .ws_transport import TransportClosed

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, sessions: RunnerSessionManager, dispatch: DispatchService, *,
                 poll_interval: float = 1, dispatch_timeout: float = 5, retry_delay: float = 5):
        self.sessions, self.dispatch = sessions, dispatch
        self.poll_interval, self.dispatch_timeout, self.retry_delay = poll_interval, dispatch_timeout, retry_delay
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop: asyncio.Task | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._capacity = asyncio.Semaphore(32)
        self._retry_after: dict[str, float] = {}

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run(), name="scheduler")

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stopping = True
        self.wake()
        if self._loop is not None:
            await self._loop
        # 让已经发出的派发在 RPC 超时内收尾，资源由 lifespan 在这之后关闭。
        await asyncio.gather(*self._tasks.values())

    async def _run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            now = asyncio.get_running_loop().time()
            self._retry_after = {key: deadline for key, deadline in self._retry_after.items() if deadline > now}
            for session in self.sessions.ready_sessions():
                runner_id = session.identity.runner_id
                if runner_id not in self._tasks and runner_id not in self._retry_after:
                    task = asyncio.create_task(self._dispatch_one(session), name=f"dispatch-{runner_id}")
                    self._tasks[runner_id] = task
            try:
                async with asyncio.timeout(self.poll_interval):
                    await self._wake.wait()
            except TimeoutError:
                pass

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
        claim = await self.dispatch.claim(identity)
        if claim is None:
            return
        if not await self._can_dispatch(session):
            # 从未进入 RPC，因此可以安全释放这次预留。
            await self.dispatch.record(identity, claim, outcome="rejected", reason="session_unavailable")
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
            # 触发重连对账；unknown 继续占用容量，迟到结果仍可补传。
            session.request_close(1011)
        if changed and outcome == "rejected" and reason in RETRYABLE_REJECTIONS:
            self._retry_after[identity.runner_id] = asyncio.get_running_loop().time() + self.retry_delay
