"""供 Nginx auth_request 查询强绑定接口的 Runner 归属。"""

import re
from hmac import compare_digest

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from evalforge.db.models import Experiment

from ..errors import APIError
from ..schemas.common import MAX_DB_ID

router = APIRouter(include_in_schema=False)
CREATE_PATH = re.compile(r"/api/v1/runners/([a-zA-Z0-9_-]{1,128})/datasets/([1-9][0-9]{0,9})/experiments")
EVENTS_PATH = re.compile(r"/api/v1/experiments/([1-9][0-9]{0,9})/jobs/[a-zA-Z0-9_-]{1,128}/events")


@router.get("/internal/runner-route", status_code=204, response_class=Response)
async def runner_route(request: Request) -> Response:
    token = request.app.state.settings.internal_route_token
    supplied = request.headers.get("X-EvalForge-Route-Token", "")
    if not token or not compare_digest(token.encode(), supplied.encode()):
        raise APIError(403, "route_lookup_forbidden", "内部归属查询不可访问")

    path = request.headers.get("X-Original-Path", "")
    if (match := CREATE_PATH.fullmatch(path)) and int(match[2]) <= MAX_DB_ID:
        runner_id = match[1]
    elif (match := EVENTS_PATH.fullmatch(path)) and (experiment_id := int(match[1])) <= MAX_DB_ID:
        try:
            async with request.app.state.database.transaction() as db:
                runner_id = await db.scalar(select(Experiment.execution_config["runner_id"].as_string()).where(
                    Experiment.id == experiment_id,
                ))
        except SQLAlchemyError as exc:
            raise APIError(503, "runner_route_unavailable", "Runner 归属暂时无法查询") from exc
        if not runner_id:
            raise APIError(403, "runner_route_missing", "该实验没有可路由的 Runner")
    else:
        raise APIError(403, "route_path_unsupported", "该路径不支持 Runner 定向路由")

    connections = request.app.state.runner_sessions.connections
    current = await connections.read_current(runner_id)
    if current is None:
        raise APIError(403, "runner_offline", "请先连接实验指定的 Runner")
    if current.phase != "ready":
        raise APIError(403, "runner_not_ready", "Runner 尚未完成同步，请稍后重试")
    if current.cloud_route_id is None:
        raise APIError(503, "runner_route_unavailable", "当前 Runner 会话缺少路由标识")
    return Response(status_code=204, headers={"X-EvalForge-Cloud-Route-Id": current.cloud_route_id,
                                               "Cache-Control": "no-store"})
