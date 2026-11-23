"""Browser REST endpoints for platform rules and dataset evaluator bindings."""

from fastapi import APIRouter, Depends, Path, Query, Request

from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..errors import APIError
from ..schemas.code_evaluators import (
    CodeEvaluatorCreate, CodeEvaluatorDetail, CodeEvaluatorDraft, CodeEvaluatorDraftSaved, CodeEvaluatorPage,
    CodeEvaluatorPatch, CodeEvaluatorPreview, CodeEvaluatorPreviewRequest, CodeEvaluatorVersionDetail,
    CodeEvaluatorVersionPage, CodeEvaluatorVersionSummary,
)
from ..schemas.common import MAX_DB_ID
from ..schemas.evaluators import (
    BuiltinEvaluatorDefinition, DatasetEvaluatorDetail, DatasetEvaluatorPage, EvaluatorConfig,
)
from ..services.evaluators import DatasetEvaluatorService, builtin_catalog
from ..services.code_evaluators import CodeEvaluatorService

router = APIRouter(prefix="/api/v1", tags=["evaluators"])


def evaluator_service(request: Request) -> DatasetEvaluatorService:
    return request.app.state.dataset_evaluators


def code_evaluator_service(request: Request) -> CodeEvaluatorService:
    return request.app.state.code_evaluators


@router.get("/evaluators/code", response_model=CodeEvaluatorPage)
async def list_code_evaluators(page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                               pagesize: int = Query(default=20, ge=1, le=100),
                               published_only: bool = False,
                               include_archived: bool = False,
                               identity: Authenticated = Depends(current_user),
                               service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.list(identity.user.id, page, pagesize, published_only, include_archived=include_archived)


@router.post("/evaluators/code", status_code=201, response_model=CodeEvaluatorDetail,
             dependencies=[Depends(require_csrf)])
async def create_code_evaluator(data: CodeEvaluatorCreate, identity: Authenticated = Depends(current_user),
                                service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.create(identity.user.id, identity.session.auth_version, data)


@router.post("/evaluators/code/preview", response_model=CodeEvaluatorPreview,
             dependencies=[Depends(current_user), Depends(require_csrf)])
async def preview_code_evaluator(data: CodeEvaluatorPreviewRequest, request: Request) -> CodeEvaluatorPreview:
    sandbox = request.app.state.snekbox
    if sandbox is None:
        raise APIError(503, "sandbox_unavailable", "评分沙箱尚未配置")
    return await sandbox.execute(data.source_code, data.context)


@router.get("/evaluators/code/{evaluator_id}", response_model=CodeEvaluatorDetail)
async def get_code_evaluator(evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                             identity: Authenticated = Depends(current_user),
                             service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.get(identity.user.id, evaluator_id)


@router.patch("/evaluators/code/{evaluator_id}", response_model=CodeEvaluatorDetail,
              dependencies=[Depends(require_csrf)])
async def update_code_evaluator(data: CodeEvaluatorPatch, evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                identity: Authenticated = Depends(current_user),
                                service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.update(identity.user.id, identity.session.auth_version, evaluator_id, data)


@router.patch("/evaluators/code/{evaluator_id}/draft", response_model=CodeEvaluatorDraftSaved,
              dependencies=[Depends(require_csrf)])
async def save_code_evaluator_draft(data: CodeEvaluatorDraft, evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                    identity: Authenticated = Depends(current_user),
                                    service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.save_draft(identity.user.id, identity.session.auth_version, evaluator_id, data.source_code)


@router.post("/evaluators/code/{evaluator_id}/archive", response_model=CodeEvaluatorDetail,
             dependencies=[Depends(require_csrf)])
async def archive_code_evaluator(evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                 identity: Authenticated = Depends(current_user),
                                 service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.archive(identity.user.id, identity.session.auth_version, evaluator_id)


@router.post("/evaluators/code/{evaluator_id}/versions", status_code=201,
             response_model=CodeEvaluatorVersionSummary, dependencies=[Depends(require_csrf)])
async def publish_code_evaluator(evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                 identity: Authenticated = Depends(current_user),
                                 service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.publish(identity.user.id, identity.session.auth_version, evaluator_id)


@router.get("/evaluators/code/{evaluator_id}/versions", response_model=CodeEvaluatorVersionPage)
async def list_code_evaluator_versions(evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                      page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                                      pagesize: int = Query(default=20, ge=1, le=100),
                                      identity: Authenticated = Depends(current_user),
                                      service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.list_versions(identity.user.id, evaluator_id, page, pagesize)


@router.get("/evaluators/code/{evaluator_id}/versions/{version_id}", response_model=CodeEvaluatorVersionDetail)
async def get_code_evaluator_version(evaluator_id: int = Path(ge=1, le=MAX_DB_ID),
                                    version_id: int = Path(ge=1, le=MAX_DB_ID),
                                    identity: Authenticated = Depends(current_user),
                                    service: CodeEvaluatorService = Depends(code_evaluator_service)):
    return await service.get_version(identity.user.id, evaluator_id, version_id)


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
async def create_dataset_evaluator(data: EvaluatorConfig, dataset_id: int = Path(ge=1, le=MAX_DB_ID),
                                  identity: Authenticated = Depends(current_user),
                                  _csrf: SessionContext = Depends(require_csrf),
                                  service: DatasetEvaluatorService = Depends(evaluator_service)):
    return await service.create(identity.user.id, identity.session.auth_version, dataset_id, data)
