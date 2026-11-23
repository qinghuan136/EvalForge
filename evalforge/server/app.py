"""FastAPI 工厂与资源生命周期；通过 Uvicorn --factory 启动。"""

from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from evalforge.db import Database, DatabaseSettings
from evalforge.redis import RedisSettings, RedisStore

from .config import ServerSettings
from .errors import install_handlers
from .evaluators.snekbox import SnekboxClient
from .routes.assistant import router as assistant_router
from .routes.assistant import routed_router as routed_assistant_router
from .routes.auth import router as auth_router
from .routes.datasets import router as datasets_router
from .routes.evaluators import router as evaluators_router
from .routes.experiment_configs import router as experiment_configs_router
from .routes.experiments import router as experiments_router
from .routes.internal_routing import router as internal_routing_router
from .routes.otlp import router as otlp_router
from .routes.runners import router as runners_router
from .routes.runner_ws import router as runner_ws_router
from .routes.traces import router as traces_router
from .rpc import JsonRpcPeer
from .runner_auth import RunnerAuth, RunnerIdentity
from .runner_connections import RunnerConnections
from .runner_sessions import RunnerSessionManager
from .scheduler import Scheduler
from .security import Passwords
from .services.assistant import AssistantService
from .services.auth import AuthService
from .services.code_evaluators import CodeEvaluatorService
from .services.datasets import DatasetService
from .services.runners import RunnerService
from .services.attempts import AttemptService
from .services.dispatch import DispatchService
from .services.evaluations import EvaluationService
from .services.evaluators import DatasetEvaluatorService
from .services.experiment_configs import ExperimentConfigService
from .services.experiments import ExperimentService
from .services.runtime_loader import RuntimeLoader
from .services.trace_ingest import TraceIngestService
from .services.traces import TraceQueryService
from .sessions import SessionStore


def create_app(settings: ServerSettings | None = None, *, database: Database | None = None,
               redis_client: Redis | None = None,
               snekbox_transport: httpx.AsyncBaseTransport | None = None,
               configure_runner_rpc: Callable[[JsonRpcPeer, RunnerIdentity], None] | None = None) -> FastAPI:
    config = settings or ServerSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as resources:
            app.state.snekbox = None
            if config.snekbox_url is not None:
                http = await resources.enter_async_context(httpx.AsyncClient(
                    base_url=config.snekbox_url, transport=snekbox_transport,
                    trust_env=False, follow_redirects=False, timeout=httpx.Timeout(15, connect=3),
                ))
                app.state.snekbox = SnekboxClient(http)
            db = database if database is not None else await resources.enter_async_context(Database(DatabaseSettings.from_env()))
            redis = redis_client
            if redis is None:
                store = await resources.enter_async_context(RedisStore(RedisSettings.from_env()))
                redis = store.client
            app.state.database = db
            app.state.sessions = SessionStore(redis, config.session_ttl_seconds)
            passwords = Passwords()
            await passwords.initialize()
            app.state.auth = AuthService(db, app.state.sessions, passwords)
            connections = RunnerConnections(redis)
            app.state.runners = RunnerService(db, connections, config.runner_websocket_url)
            app.state.runner_auth = RunnerAuth(db, redis)
            attempts = AttemptService(db)
            app.state.runner_sessions = RunnerSessionManager(
                connections, attempts, runner_auth=app.state.runner_auth)
            resources.push_async_callback(app.state.runner_sessions.close)
            scheduler = Scheduler(DispatchService(db), RuntimeLoader(db), EvaluationService(db, app.state.snekbox))
            app.state.scheduler = scheduler
            app.state.runner_sessions.on_ready = scheduler.session_ready
            app.state.runner_sessions.on_closed = scheduler.session_closed
            app.state.runner_sessions.on_complete = scheduler.task_completed
            app.state.datasets = DatasetService(db)
            app.state.dataset_evaluators = DatasetEvaluatorService(db)
            app.state.code_evaluators = CodeEvaluatorService(db)
            app.state.experiment_configs = ExperimentConfigService(db)
            app.state.experiments = ExperimentService(
                db, connections, on_created=scheduler.experiment_created, on_cancelled=scheduler.experiment_cancelled)
            app.state.trace_ingest = TraceIngestService(db)
            app.state.trace_queries = TraceQueryService(db)
            app.state.assistant = AssistantService(
                db, app.state.experiments, config, runners=app.state.runners,
                datasets=app.state.datasets, traces=app.state.trace_queries,
                experiment_configs=app.state.experiment_configs, has_ready_runner=scheduler.has_ready_runner,
            )
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
        path = request.url.path
        if path in ("/api/v1/datasets", "/api/v1/traces", "/api/v1/experiment-runs") or path.startswith((
            "/api/v1/auth/", "/api/v1/runners", "/api/v1/datasets/", "/api/v1/experiments/",
            "/api/v1/evaluators/", "/api/v1/assistant/", "/api/v1/traces/", "/api/v1/experiment-configs",
        )):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_router)
    app.include_router(assistant_router)
    app.include_router(routed_assistant_router)
    app.include_router(datasets_router)
    app.include_router(evaluators_router)
    app.include_router(experiments_router)
    app.include_router(experiment_configs_router)
    app.include_router(internal_routing_router)
    app.include_router(runners_router)
    app.include_router(runner_ws_router)
    app.include_router(otlp_router)
    app.include_router(traces_router)
    return app
