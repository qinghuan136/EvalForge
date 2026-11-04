"""Runner REST 管理；使用浏览器登录，写接口要求 CSRF。"""

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from ..dependencies import Authenticated, current_user, require_csrf
from ..schemas.runners import RunnerCreateResponse, RunnerName, RunnerPage, RunnerResponse
from ..services.runners import RunnerService

router = APIRouter(prefix="/api/v1/runners", tags=["runners"])


def runner_service(request: Request) -> RunnerService:
    return request.app.state.runners


@router.post("", status_code=201, response_model=RunnerCreateResponse, dependencies=[Depends(require_csrf)])
async def create_runner(data: RunnerName, identity: Authenticated = Depends(current_user),
                        service: RunnerService = Depends(runner_service)):
    return await service.create(identity.user.id, identity.session.auth_version, data.name)


@router.get("", response_model=RunnerPage)
async def list_runners(page: int = Query(default=1, ge=1, le=2147483647),
                       pagesize: int = Query(default=20, ge=1, le=100),
                       identity: Authenticated = Depends(current_user), service: RunnerService = Depends(runner_service)):
    return await service.list(identity.user.id, page, pagesize)


@router.get("/{runner_id}", response_model=RunnerResponse)
async def get_runner(runner_id: str = Path(min_length=1, max_length=128),
                      identity: Authenticated = Depends(current_user), service: RunnerService = Depends(runner_service)):
    return await service.get(identity.user.id, runner_id)


@router.patch("/{runner_id}", response_model=RunnerResponse, dependencies=[Depends(require_csrf)])
async def rename_runner(data: RunnerName, runner_id: str = Path(min_length=1, max_length=128),
                        identity: Authenticated = Depends(current_user), service: RunnerService = Depends(runner_service)):
    return await service.rename(identity.user.id, identity.session.auth_version, runner_id, data.name)


@router.delete("/{runner_id}", status_code=204, response_class=Response, dependencies=[Depends(require_csrf)])
async def delete_runner(runner_id: str = Path(min_length=1, max_length=128),
                        identity: Authenticated = Depends(current_user), service: RunnerService = Depends(runner_service)):
    await service.delete(identity.user.id, identity.session.auth_version, runner_id)
    return Response(status_code=204)
