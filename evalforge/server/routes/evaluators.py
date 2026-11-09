"""Browser REST endpoints for platform rules and dataset evaluator bindings."""

from fastapi import APIRouter, Depends, Path, Query, Request

from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..schemas.common import MAX_DB_ID
from ..schemas.evaluators import (
    BuiltinEvaluatorDefinition, DatasetEvaluatorCreate, DatasetEvaluatorDetail, DatasetEvaluatorPage,
)
from ..services.evaluators import DatasetEvaluatorService, builtin_catalog

router = APIRouter(prefix="/api/v1", tags=["evaluators"])


def evaluator_service(request: Request) -> DatasetEvaluatorService:
    return request.app.state.dataset_evaluators


@router.get("/evaluators/builtin", response_model=list[BuiltinEvaluatorDefinition], dependencies=[Depends(current_user)])
async def list_builtin():
    return builtin_catalog()


@router.get("/datasets/{dataset_id}/evaluators", response_model=DatasetEvaluatorPage)
async def list_dataset_evaluators(dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                                  page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                                  pagesize: int = Query(default=20, ge=1, le=100),
                                  identity: Authenticated = Depends(current_user),
                                  service: DatasetEvaluatorService = Depends(evaluator_service)):
    return await service.list(identity.user.id, dataset_id, page, pagesize)


@router.post("/datasets/{dataset_id}/evaluators", status_code=201, response_model=DatasetEvaluatorDetail)
async def create_dataset_evaluator(data: DatasetEvaluatorCreate, dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                                  identity: Authenticated = Depends(current_user),
                                  _csrf: SessionContext = Depends(require_csrf),
                                  service: DatasetEvaluatorService = Depends(evaluator_service)):
    return await service.create(identity.user.id, identity.session.auth_version, dataset_id, data)
