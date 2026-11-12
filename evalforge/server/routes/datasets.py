"""浏览器数据集导入接口。"""

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from python_multipart.exceptions import FormParserError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from ..dataset_import import parse_dataset_upload
from ..dependencies import Authenticated, SessionContext, current_user, require_csrf
from ..errors import APIError
from ..schemas.datasets import MAX_CSV_COLUMNS, MAX_DATASET_UPLOAD_BYTES, DatasetImportOptions, DatasetImportResult
from ..services.datasets import DatasetService

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

_COLUMN_FIELDS = ("input_keys", "output_keys", "metadata_keys")
_OPTION_SCHEMA = DatasetImportOptions.model_json_schema()
_MULTIPART_SCHEMA = {
    **_OPTION_SCHEMA,
    "required": ["file", *_OPTION_SCHEMA["required"]],
    "properties": {"file": {"type": "string", "format": "binary"}, **_OPTION_SCHEMA["properties"]},
}


class _UploadTooLarge(MultiPartException):
    pass


async def _bounded_upload(request: Request) -> AsyncGenerator[bytes, None]:
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_DATASET_UPLOAD_BYTES + 64 * 1024:
                # MultiPartException 让 Starlette 同时清理解析到一半的临时文件。
                raise _UploadTooLarge("multipart body too large")
            yield chunk
    except ClientDisconnect:
        raise MultiPartException("client disconnected during upload") from None


def _upload_too_large() -> APIError:
    return APIError(413, "dataset_file_too_large", "文件不能超过 5 MiB")


async def _read_form(request: Request) -> FormData:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "multipart/form-data":
        raise APIError(415, "unsupported_dataset_upload", "请使用 multipart/form-data 上传")
    try:
        return await MultiPartParser(request.headers, _bounded_upload(request), max_files=1,
                                     max_fields=MAX_CSV_COLUMNS + 3).parse()
    except _UploadTooLarge:
        raise _upload_too_large() from None
    except (MultiPartException, FormParserError):
        raise APIError(400, "invalid_dataset_upload", "上传表单格式不合法") from None


def _options(form: FormData) -> DatasetImportOptions:
    fields = {key: form.getlist(key) for key in form if key != "file"}
    if any(len(values) != 1 for key, values in fields.items() if key not in _COLUMN_FIELDS):
        raise APIError(422, "invalid_request", "导入表单字段不合法")
    try:
        return DatasetImportOptions.model_validate({
            key: values if key in _COLUMN_FIELDS else values[0] for key, values in fields.items()
        })
    except ValidationError:
        raise APIError(422, "invalid_request", "导入表单字段不合法") from None


def dataset_service(request: Request) -> DatasetService:
    return request.app.state.datasets


@router.post("/import", status_code=201, response_model=DatasetImportResult, openapi_extra={
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": _MULTIPART_SCHEMA}}},
})
async def import_dataset(request: Request, identity: Authenticated = Depends(current_user),
                         _csrf: SessionContext = Depends(require_csrf),
                         service: DatasetService = Depends(dataset_service)):
    # 登录与 CSRF 校验完成后才开始读取请求体。
    form = await _read_form(request)
    try:
        files = form.getlist("file")
        if len(files) != 1 or not isinstance(file := files[0], UploadFile):
            raise APIError(422, "invalid_request", "请选择一个要导入的文件")
        if file.size is not None and file.size > MAX_DATASET_UPLOAD_BYTES:
            raise _upload_too_large()
        options = _options(form)
        content = await file.read()
    finally:
        await form.close()
    drafts = await run_in_threadpool(parse_dataset_upload, content, options)
    return await service.import_dataset(identity.user.id, identity.session.auth_version, options, drafts)
