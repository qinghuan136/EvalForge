"""请求资源、来源校验、CSRF 和当前用户依赖。"""

from dataclasses import dataclass, field

from fastapi import Depends, Header, Request

from evalforge.db.models import User

from .config import ServerSettings, origin_of
from .errors import APIError
from .services.auth import AuthService
from .sessions import BrowserSession, SessionStore, TOKEN_PATTERN


def settings(request: Request) -> ServerSettings:
    return request.app.state.settings


def session_store(request: Request) -> SessionStore:
    return request.app.state.sessions


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth


def verify_origin(request: Request, optional: bool = False) -> None:
    value = request.headers.get("origin")
    referer = request.headers.get("referer")
    if value is None and referer is None and optional:
        return
    try:
        source = origin_of(value if value is not None else referer or "")
        # Origin 不能含路径；Referer 可以。
        if value is not None and source != value:
            raise ValueError()
    except ValueError:
        raise APIError(403, "origin_forbidden", "请求来源不受信任") from None
    if source not in settings(request).allowed_origins:
        raise APIError(403, "origin_forbidden", "请求来源不受信任")


@dataclass(frozen=True)
class SessionContext:
    token: str | None = field(repr=False)
    record: BrowserSession | None


async def browser_session(request: Request, store: SessionStore = Depends(session_store)) -> SessionContext:
    token = request.cookies.get(settings(request).cookie_name)
    return SessionContext(token, await store.load(token))


async def require_csrf(request: Request, context: SessionContext = Depends(browser_session),
                       csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> SessionContext:
    verify_origin(request)
    if context.record is None:
        raise APIError(403, "csrf_invalid", "请先获取 CSRF Token")
    SessionStore.verify_csrf(context.record, csrf_token)
    return context


async def logout_csrf(request: Request, context: SessionContext = Depends(browser_session),
                      csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> SessionContext:
    verify_origin(request)
    provided = csrf_token
    if context.record is not None:
        SessionStore.verify_csrf(context.record, provided)
    elif provided is None or not TOKEN_PATTERN.fullmatch(provided):
        raise APIError(403, "csrf_invalid", "CSRF 校验失败")
    return context


@dataclass(frozen=True)
class Authenticated:
    user: User
    session: BrowserSession


async def current_user(context: SessionContext = Depends(browser_session),
                       service: AuthService = Depends(auth_service)) -> Authenticated:
    user = await service.current_user(context.record)
    return Authenticated(user, context.record)


def client_address(request: Request) -> str:
    # 不直接信任任意调用方传入的 X-Forwarded-For；代理信任范围由 ASGI 服务配置。
    return request.client.host if request.client else "unknown"
