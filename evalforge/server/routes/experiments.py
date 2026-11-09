"""浏览器实验 REST 接口：创建、按数据集分页查询、详情。"""

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..schemas.experiments import MAX_DB_ID, ExperimentCreate, ExperimentDetail, ExperimentPage
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
