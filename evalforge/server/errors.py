"""统一错误响应；不回显密码、请求原文或基础设施异常。"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def business_error(request: Request, exc: APIError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return error_response(422, "invalid_request", "请求字段不合法")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(exc.status_code, "http_error", "请求无法处理")

    async def unavailable(request: Request, exc: Exception):
        logger.warning("Authentication dependency unavailable error_type=%s", type(exc).__name__)
        return error_response(503, "service_unavailable", "认证服务暂时不可用")

    app.add_exception_handler(RedisError, unavailable)
    app.add_exception_handler(SQLAlchemyError, unavailable)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        logger.error("Authentication request failed error_type=%s", type(exc).__name__)
        return error_response(500, "internal_error", "服务内部错误")
