"""可复用实验配置和版本历史的 REST 接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from ..dependencies import Authenticated, current_user, require_csrf
from ..schemas.common import MAX_DB_ID
from ..schemas.experiments import (
    ExperimentConfigDetail, ExperimentConfigPage, ExperimentConfigRevision, ExperimentConfigSave, ExperimentPage,
)
from ..services.experiment_configs import ExperimentConfigService
from ..services.experiments import ExperimentService
from .experiments import experiment_service

router = APIRouter(prefix="/api/v1/experiment-configs", tags=["experiment-configs"])


def config_service(request: Request) -> ExperimentConfigService:
    return request.app.state.experiment_configs


@router.post("", status_code=201, response_model=ExperimentConfigDetail, dependencies=[Depends(require_csrf)])
async def create_config(data: ExperimentConfigSave, identity: Authenticated = Depends(current_user),
                        service: ExperimentConfigService = Depends(config_service)):
    return await service.create(identity.user.id, identity.session.auth_version, data)


@router.get("", response_model=ExperimentConfigPage)
async def list_configs(page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                       pagesize: int = Query(default=20, ge=1, le=100),
                       dataset_id: int | None = Query(default=None, ge=1, le=MAX_DB_ID),
                       identity: Authenticated = Depends(current_user),
                       service: ExperimentConfigService = Depends(config_service)):
    return await service.list(identity.user.id, page, pagesize, dataset_id)


@router.get("/{group_id}", response_model=ExperimentConfigDetail)
async def get_config(group_id: UUID, identity: Authenticated = Depends(current_user),
                     service: ExperimentConfigService = Depends(config_service)):
    return await service.get(identity.user.id, group_id)


@router.get("/{group_id}/versions", response_model=ExperimentConfigPage)
async def list_versions(group_id: UUID, page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                        pagesize: int = Query(default=20, ge=1, le=100),
                        identity: Authenticated = Depends(current_user),
                        service: ExperimentConfigService = Depends(config_service)):
    return await service.versions(identity.user.id, group_id, page, pagesize)


@router.post("/{group_id}/versions", status_code=201, response_model=ExperimentConfigDetail,
             dependencies=[Depends(require_csrf)], responses={200: {"model": ExperimentConfigDetail}})
async def save_version(group_id: UUID, data: ExperimentConfigRevision, response: Response,
                       identity: Authenticated = Depends(current_user),
                       service: ExperimentConfigService = Depends(config_service)):
    config, changed = await service.revise(identity.user.id, identity.session.auth_version, group_id, data)
    if not changed:
        response.status_code = 200
    return config


@router.get("/{group_id}/runs", response_model=ExperimentPage)
async def list_runs(group_id: UUID, page: int = Query(default=1, ge=1, le=MAX_DB_ID),
                    pagesize: int = Query(default=20, ge=1, le=100),
                    version: int | None = Query(default=None, ge=1, le=MAX_DB_ID),
                    identity: Authenticated = Depends(current_user),
                    service: ExperimentService = Depends(experiment_service)):
    return await service.list_runs(identity.user.id, page, pagesize, group_id=group_id, version=version)
