"""云端会话阶段转换规则；纯判断，不读写 Redis，也不修改会话对象。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class SessionState(StrEnum):
    CONNECTED = "connected"
    SYNCING = "syncing"
    READY = "ready"
    CLOSED = "closed"


class SessionEvent(StrEnum):
    HELLO_CONFIRMED = "hello_confirmed"
    READY_REQUESTED = "ready_requested"
    LEASE_RENEWED = "lease_renewed"
    CLOSE = "close"


@dataclass(frozen=True)
class SessionContext:
    """由云端可信事实构建；is_current 是本次操作的校验结果，不是永久授权。"""

    is_current: bool = False
    hello_validated: bool = False
    sync_processed: bool = False
    pending_uploads_count: int = 0

    def __post_init__(self) -> None:
        for name in ("is_current", "hello_validated", "sync_processed"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if type(self.pending_uploads_count) is not int:
            raise TypeError("pending_uploads_count must be an int")
        if self.pending_uploads_count < 0:
            raise ValueError("pending_uploads_count must not be negative")


class TransitionRejected(ValueError):
    def __init__(self, current: SessionState, event: SessionEvent, reason: str):
        super().__init__(f"{current.value} + {event.value}: {reason}")
        self.current, self.event, self.reason = current, event, reason


@dataclass(frozen=True)
class TransitionPreview:
    current: SessionState
    event: SessionEvent
    next_state: SessionState | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.next_state is None) == (self.reason is None):
            raise ValueError("Preview requires either a next state or a rejection reason")

    @property
    def allowed(self) -> bool:
        return self.reason is None


def always(context: SessionContext) -> bool:
    return True


def hello_confirmed(context: SessionContext) -> bool:
    return context.hello_validated and context.is_current


def current_session(context: SessionContext) -> bool:
    return context.is_current


def sync_finished(context: SessionContext) -> bool:
    return context.is_current and context.sync_processed and context.pending_uploads_count == 0


Guard = Callable[[SessionContext], bool]
Rule = tuple[Guard, SessionState]

# (当前状态, 事件): (条件, 目标状态)。没有列出的转换一律拒绝。
# HELLO_CONFIRMED 在存储确认后提交；LEASE_RENEWED 只校验续租结果，不改变阶段。
SESSION_TRANSITIONS: Mapping[tuple[SessionState, SessionEvent], Rule] = MappingProxyType({
    (SessionState.CONNECTED, SessionEvent.HELLO_CONFIRMED): (hello_confirmed, SessionState.SYNCING),
    (SessionState.SYNCING, SessionEvent.READY_REQUESTED): (sync_finished, SessionState.READY),
    (SessionState.READY, SessionEvent.READY_REQUESTED): (current_session, SessionState.READY),
    (SessionState.SYNCING, SessionEvent.LEASE_RENEWED): (current_session, SessionState.SYNCING),
    (SessionState.READY, SessionEvent.LEASE_RENEWED): (current_session, SessionState.READY),
    **{(state, SessionEvent.CLOSE): (always, SessionState.CLOSED) for state in SessionState},
})


def _find_rule(current: SessionState, event: SessionEvent) -> Rule | None:
    if not isinstance(current, SessionState):
        raise TypeError("current must be a SessionState")
    if not isinstance(event, SessionEvent):
        raise TypeError("event must be a SessionEvent")
    return SESSION_TRANSITIONS.get((current, event))


def validate_session_event(current: SessionState, event: SessionEvent) -> None:
    """外部操作前检查阶段是否允许该事件；不执行 guard，不授予转换许可。"""
    if _find_rule(current, event) is None:
        raise TransitionRejected(current, event, "unsupported_transition")


def preview_session_transition(current: SessionState, event: SessionEvent,
                               context: SessionContext) -> TransitionPreview:
    """根据当前事实预览转换；业务拒绝作为结果返回，不修改状态或外部存储。"""
    rule = _find_rule(current, event)
    if not isinstance(context, SessionContext):
        raise TypeError("context must be a SessionContext")
    if rule is None:
        return TransitionPreview(current, event, None, "unsupported_transition")
    guard, target = rule
    if not guard(context):
        return TransitionPreview(current, event, None, "guard_rejected")
    return TransitionPreview(current, event, target)


def next_session_state(current: SessionState, event: SessionEvent, context: SessionContext) -> SessionState:
    """严格判断入口：成功返回目标状态，业务拒绝抛出 TransitionRejected。"""
    preview = preview_session_transition(current, event, context)
    if preview.reason is not None:
        raise TransitionRejected(current, event, preview.reason)
    assert preview.next_state is not None
    return preview.next_state
