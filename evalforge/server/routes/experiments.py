"""浏览器实验 REST 接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..schemas.common import MAX_DB_ID, StoredText
from ..schemas.experiments import ExperimentCreate, ExperimentDetail, ExperimentPage, JobDetail, JobPage, JobStatus
from ..services.experiments import ExperimentService

router = APIRouter(prefix="/api/v1", tags=["experiments"])


def experiment_service(request: Request) -> ExperimentService:
    return request.app.state.experiments


@router.post("/datasets/{dataset_id}/experiments", status_code=201, response_model=ExperimentDetail)
async def create_experiment(data: ExperimentCreate, response: Response, dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                            identity: Authenticated = Depends(current_user),
                            _csrf: SessionContext = Depends(require_csrf),
                            service: ExperimentService = Depends(experiment_service)):
    experiment = await service.create(identity.user.id, identity.session.auth_version, dataset_id, data)
    response.headers["Location"] = f"/api/v1/experiments/{experiment.experiment_id}"
    return experiment


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


@router.post("/experiments/{experiment_id}/cancel", response_model=ExperimentDetail)
async def cancel_experiment(experiment_id: int = Path(ge=1, le=MAX_DB_ID),
                            identity: Authenticated = Depends(current_user),
                            _csrf: SessionContext = Depends(require_csrf),
                            service: ExperimentService = Depends(experiment_service)):
    return await service.cancel(identity.user.id, identity.session.auth_version, experiment_id)
