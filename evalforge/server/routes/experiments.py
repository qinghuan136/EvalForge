"""浏览器实验 REST 接口。"""

from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..errors import APIError
from ..experiment_events import event_frame
from ..schemas.common import MAX_DB_ID, StoredText
from ..schemas.experiments import (
    ExperimentCreate, ExperimentDetail, ExperimentPage, ExperimentRunCreate, ExperimentStatus, JobDetail, JobPage,
    JobStatus,
)
from ..services.experiments import ExperimentService
from ..services.experiment_lifecycle import SETTLED_JOBS

router = APIRouter(prefix="/api/v1", tags=["experiments"])


def experiment_service(request: Request) -> ExperimentService:
    return request.app.state.experiments


def _check_local_runner(request: Request, runner_id: str, owner_id: int) -> None:
    if not request.app.state.scheduler.has_ready_runner(runner_id, owner_id):
        raise APIError(503, "runner_not_ready_here", "当前实例没有就绪的 Runner 会话，请稍后重试")


async def _create_experiment(data: ExperimentCreate, response: Response, dataset_id: int,
                             identity: Authenticated, service: ExperimentService) -> ExperimentDetail:
    experiment = await service.create(identity.user.id, identity.session.auth_version, dataset_id, data)
    response.headers["Location"] = f"/api/v1/experiments/{experiment.experiment_id}"
    return experiment


@router.post("/datasets/{dataset_id}/experiments", status_code=201, response_model=ExperimentDetail)
async def create_experiment(data: ExperimentCreate, request: Request, response: Response,
                            dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                            identity: Authenticated = Depends(current_user),
                            _csrf: SessionContext = Depends(require_csrf),
                            service: ExperimentService = Depends(experiment_service)):
    if request.app.state.settings.internal_route_token:
        _check_local_runner(request, data.execution_config.runner_id, identity.user.id)
    return await _create_experiment(data, response, dataset_id, identity, service)


async def require_local_runner(data: ExperimentCreate, request: Request,
                               runner_id: Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$")],
                               identity: Authenticated = Depends(current_user)) -> ExperimentCreate:
    if data.execution_config.runner_id != runner_id:
        raise APIError(422, "runner_id_mismatch", "路径和请求体中的 Runner ID 必须一致")
    _check_local_runner(request, runner_id, identity.user.id)
    return data


@router.post("/runners/{runner_id}/datasets/{dataset_id}/experiments", status_code=201, response_model=ExperimentDetail)
async def create_routed_experiment(response: Response, dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                                   identity: Authenticated = Depends(current_user),
                                   _csrf: SessionContext = Depends(require_csrf),
                                   data: ExperimentCreate = Depends(require_local_runner),
                                   service: ExperimentService = Depends(experiment_service)):
    return await _create_experiment(data, response, dataset_id, identity, service)


@router.post("/runners/{runner_id}/experiment-configs/{config_id}/runs", status_code=201,
             response_model=ExperimentDetail, dependencies=[Depends(require_csrf)])
async def start_run(data: ExperimentRunCreate, request: Request, response: Response,
                    runner_id: Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$")],
                    config_id: int = Path(ge=1, le=MAX_DB_ID),
                    identity: Authenticated = Depends(current_user),
                    service: ExperimentService = Depends(experiment_service)):
    result = await service.start_run(identity.user.id, identity.session.auth_version, config_id, data,
                                     runner_id=runner_id, check_local=partial(_check_local_runner, request))
    response.headers["Location"] = f"/api/v1/experiments/{result.experiment_id}"
    return result


@router.get("/experiment-runs", response_model=ExperimentPage)
async def list_experiment_runs(page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                               pagesize: int = Query(default=20, ge=1, le=100),
                               group_id: UUID | None = Query(default=None),
                               version: int | None = Query(default=None, ge=1, le=MAX_DB_ID),
                               status: ExperimentStatus | None = Query(default=None),
                               identity: Authenticated = Depends(current_user),
                               service: ExperimentService = Depends(experiment_service)):
    return await service.list_runs(identity.user.id, page, pagesize, group_id=group_id, version=version, status=status)


@router.get("/datasets/{dataset_id}/experiments", response_model=ExperimentPage)
async def list_experiments(dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                           page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                           pagesize: int = Query(default=20, ge=1, le=100),
                           identity: Authenticated = Depends(current_user),
                           service: ExperimentService = Depends(experiment_service)):
    return await service.list(identity.user.id, dataset_id, page, pagesize)


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                         identity: Authenticated = Depends(current_user),
                         service: ExperimentService = Depends(experiment_service)):
    return await service.get(identity.user.id, experiment_id)


@router.get("/experiments/{experiment_id}/jobs", response_model=JobPage)
async def list_jobs(experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                    page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                    pagesize: int = Query(default=20, ge=1, le=100),
                    status: JobStatus | None = Query(default=None),
                    identity: Authenticated = Depends(current_user),
                    service: ExperimentService = Depends(experiment_service)):
    return await service.list_jobs(identity.user.id, experiment_id, page, pagesize, status)


@router.get("/experiments/{experiment_id}/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: Annotated[StoredText, Path(min_length=1, max_length=128, pattern=r"\S")],
                  experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                  identity: Authenticated = Depends(current_user),
                  service: ExperimentService = Depends(experiment_service)):
    return await service.get_job(identity.user.id, experiment_id, job_id)


@router.get("/experiments/{experiment_id}/jobs/{job_id}/events", response_class=StreamingResponse)
async def job_events(request: Request,
                     job_id: Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$")],
                     experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                     identity: Authenticated = Depends(current_user),
                     service: ExperimentService = Depends(experiment_service)) -> Response:
    owner_id = identity.user.id
    status, runner_id = await service.job_preview_state(owner_id, experiment_id, job_id)
    if runner_id is not None and status in {"assigned", "running"}:
        preview = request.app.state.scheduler.job_preview(runner_id, owner_id, experiment_id, job_id)
        if preview is not None:
            return StreamingResponse(preview.events(), media_type="text/event-stream", headers={
                "Cache-Control": "no-store", "X-Accel-Buffering": "no",
            })
        # Job 可能在查状态后完成并移除了缓冲。
        status, runner_id = await service.job_preview_state(owner_id, experiment_id, job_id)
    if status in SETTLED_JOBS:
        return Response(event_frame("done", {"status": status}), media_type="text/event-stream")
    if runner_id is None or status == "queued":
        return Response(status_code=204)
    raise APIError(503, "job_preview_unavailable", "当前实例无法提供 Job 预览，请稍后重试")


@router.post("/experiments/{experiment_id}/cancel", response_model=ExperimentDetail)
async def cancel_experiment(experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                            identity: Authenticated = Depends(current_user),
                            _csrf: SessionContext = Depends(require_csrf),
                            service: ExperimentService = Depends(experiment_service)):
    return await service.cancel(identity.user.id, identity.session.auth_version, experiment_id)
