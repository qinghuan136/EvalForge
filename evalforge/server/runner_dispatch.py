"""执行一次 Task 派发或取消；数据库事务和 RPC 等待分开。"""

from collections.abc import Callable
from datetime import datetime, timezone
import logging

from redis.exceptions import RedisError

from .errors import APIError
from .experiment_events import PreviewTarget
from .rpc import RpcError, RpcProtocolError, RpcRequestTooLarge
from .runner_sessions import RunnerSession
from .services.dispatch import CancelRequest, DispatchClaim, DispatchService, DispatchUpdate
from .ws_transport import TransportClosed

logger = logging.getLogger(__name__)


async def send_dispatch(session: RunnerSession, service: DispatchService, claim: DispatchClaim,
                        timeout: float) -> DispatchUpdate:
    identity = session.identity
    if not session.can_dispatch:
        return await service.record(identity, claim, outcome="rejected", reason="session_unavailable")
    try:
        await session.check_current()
    except (APIError, RedisError, TimeoutError):
        return await service.record(identity, claim, outcome="rejected", reason="session_unavailable")
    skipped = await service.prepare_send(identity, claim)
    if skipped is not None:
        return skipped
    if not session.can_dispatch:
        return await service.record(identity, claim, outcome="rejected", reason="session_unavailable")
    target = None
    if claim.payload.get("stream_output"):
        target = PreviewTarget(claim.experiment_id, claim.key.job_id, claim.key.attempt_id)
    session.set_preview_target(target)
    sent_at = datetime.now(timezone.utc)
    try:
        reply = await session.peer.call("jobs.dispatch", claim.payload, timeout=timeout)
    except RpcRequestTooLarge:
        outcome, reason = "rejected", "dispatch_payload_too_large"
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
    if outcome != "accepted":
        session.clear_preview_target(claim.key)
    update = await service.record(identity, claim, outcome=outcome, reason=reason,
                                  dispatched_at=None if reason == "dispatch_payload_too_large" else sent_at)
    if update.changed and outcome == "unknown":
        session.request_close(1011)
    return update


async def send_cancel(session: RunnerSession, request: CancelRequest, timeout: float,
                      is_pending: Callable[[], bool]) -> bool:
    """返回 True 表示不用重试；最终状态仍由 jobs.complete 写入。"""
    await session.check_current()
    if not is_pending():
        return True
    try:
        reply = await session.peer.call("jobs.cancel", {**request.key.model_dump(), "reason": request.reason},
                                        timeout=timeout)
    except (TimeoutError, TransportClosed, RpcError, RpcProtocolError):
        return False
    if isinstance(reply, dict):
        if reply.get("accepted") is True:
            return True
        if reply.get("reason") == "attempt_not_found":
            session.request_close(1011)
            return True
    return False
