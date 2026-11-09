"""FastAPI 工厂与资源生命周期；通过 Uvicorn --factory 启动。"""

from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from evalforge.db import Database, DatabaseSettings
from evalforge.redis import RedisSettings, RedisStore

from .config import ServerSettings
from .errors import install_handlers
from .routes.auth import router as auth_router
from .routes.evaluators import router as evaluators_router
from .routes.experiments import router as experiments_router
from .routes.runners import router as runners_router
from .routes.runner_ws import router as runner_ws_router
from .rpc import JsonRpcPeer
from .runner_auth import RunnerAuth, RunnerIdentity
from .runner_connections import RunnerConnections
from .runner_sessions import RunnerSessionManager
from .scheduler import Scheduler
from .security import Passwords
from .services.auth import AuthService
from .services.runners import RunnerService
from .services.attempts import AttemptService
from .services.dispatch import DispatchService
from .services.evaluators import DatasetEvaluatorService
from .services.experiments import ExperimentService
from .sessions import SessionStore


def create_app(settings: ServerSettings | None = None, *, database: Database | None = None,
               redis_client: Redis | None = None,
               configure_runner_rpc: Callable[[JsonRpcPeer, RunnerIdentity], None] | None = None) -> FastAPI:
    config = settings or ServerSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as resources:
            db = database if database is not None else await resources.enter_async_context(Database(DatabaseSettings.from_env()))
            redis = redis_client
            if redis is None:
                store = await resources.enter_async_context(RedisStore(RedisSettings.from_env()))
                redis = store.client
            app.state.sessions = SessionStore(redis, config.session_ttl_seconds)
            passwords = Passwords()
            await passwords.initialize()
            app.state.auth = AuthService(db, app.state.sessions, passwords)
            connections = RunnerConnections(redis)
            app.state.runners = RunnerService(db, connections, config.runner_websocket_url)
            app.state.runner_auth = RunnerAuth(db)
            app.state.runner_sessions = RunnerSessionManager(connections, AttemptService(db))
            resources.push_async_callback(app.state.runner_sessions.close)
            scheduler = Scheduler(app.state.runner_sessions, DispatchService(db))
            app.state.runner_sessions.on_work_available = scheduler.wake
            app.state.dataset_evaluators = DatasetEvaluatorService(db)
            app.state.experiments = ExperimentService(db, scheduler.wake)
            scheduler.start()
            resources.push_async_callback(scheduler.close)
            # 注入资源由调用方管理；应用创建的资源由 AsyncExitStack 按逆序关闭。
            yield

    app = FastAPI(title="EvalForge Cloud", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.configure_runner_rpc = configure_runner_rpc
    install_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=list(config.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])

    @app.middleware("http")
    async def no_auth_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/auth/", "/api/v1/runners", "/api/v1/datasets/",
                                        "/api/v1/experiments/", "/api/v1/evaluators/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_router)
    app.include_router(evaluators_router)
    app.include_router(experiments_router)
    app.include_router(runners_router)
    app.include_router(runner_ws_router)
    return app
