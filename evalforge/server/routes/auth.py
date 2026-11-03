"""登录鉴权路由；业务流程与 Redis 存储分别位于 services/auth.py 和 sessions.py。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response

from ..dependencies import (
    Authenticated, SessionContext, auth_service, browser_session, client_address,
    current_user, logout_csrf, require_csrf, session_store, settings, verify_origin,
)
from ..schemas.auth import (
    CSRFResponse, LoginRequest, LoginResponse, PasswordRequest, RegisteredResponse,
    RegisterRequest, SessionResponse, UserResponse,
)
from ..services.auth import AuthService
from ..sessions import BrowserSession, SessionStore

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def set_session_cookie(request: Request, response: Response, token: str, record: BrowserSession) -> None:
    config = settings(request)
    remaining = max(1, int((record.expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(config.cookie_name, token, max_age=remaining, expires=record.expires_at,
                        httponly=True, secure=config.cookie_secure, samesite="lax", path="/")


def clear_session_cookie(request: Request, response: Response) -> None:
    config = settings(request)
    response.delete_cookie(config.cookie_name, path="/", httponly=True,
                           secure=config.cookie_secure, samesite="lax")


@router.get("/csrf", response_model=CSRFResponse)
async def csrf(request: Request, response: Response, context: SessionContext = Depends(browser_session),
               store: SessionStore = Depends(session_store)):
    verify_origin(request, optional=True)
    await store.limit("csrf-ip", client_address(request), 60)
    record = context.record
    if record is None:
        token, record = await store.create_anonymous()
        set_session_cookie(request, response, token, record)
    return CSRFResponse(csrf_token=record.csrf_token)


@router.post("/register", status_code=201, response_model=RegisteredResponse)
async def register(data: RegisterRequest, request: Request, context: SessionContext = Depends(require_csrf),
                   service: AuthService = Depends(auth_service), store: SessionStore = Depends(session_store)):
    await store.limit("register-ip", client_address(request), settings(request).register_limit, 300)
    user = await service.register(data)
    return RegisteredResponse(user=UserResponse.model_validate(user))


@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest, request: Request, response: Response,
                context: SessionContext = Depends(require_csrf), service: AuthService = Depends(auth_service),
                store: SessionStore = Depends(session_store)):
    await store.limit("login-ip", client_address(request), settings(request).login_limit)
    await store.limit("login-name", data.username, settings(request).login_limit)
    user, token, record = await service.login(data.username, data.password.get_secret_value(), context.token)
    set_session_cookie(request, response, token, record)
    return LoginResponse(user=UserResponse.model_validate(user), session_expires_at=record.expires_at,
                         csrf_token=record.csrf_token)


@router.get("/me", response_model=SessionResponse)
async def me(identity: Authenticated = Depends(current_user)):
    return SessionResponse(user=UserResponse.model_validate(identity.user), session_expires_at=identity.session.expires_at)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, context: SessionContext = Depends(logout_csrf),
                 store: SessionStore = Depends(session_store)):
    if context.token:
        await store.delete(context.token)
    clear_session_cookie(request, response)


@router.post("/logout-all", status_code=204, dependencies=[Depends(require_csrf)])
async def logout_all(request: Request, response: Response, identity: Authenticated = Depends(current_user),
                     service: AuthService = Depends(auth_service)):
    await service.logout_all(identity.user, identity.session)
    clear_session_cookie(request, response)


@router.post("/password", status_code=204, dependencies=[Depends(require_csrf)])
async def password(data: PasswordRequest, request: Request, response: Response,
                   identity: Authenticated = Depends(current_user), service: AuthService = Depends(auth_service),
                   store: SessionStore = Depends(session_store)):
    await store.limit("password-user", str(identity.user.id), 5, 300)
    await service.change_password(identity.user, identity.session, data.current_password.get_secret_value(),
                                  data.new_password.get_secret_value())
    clear_session_cookie(request, response)
