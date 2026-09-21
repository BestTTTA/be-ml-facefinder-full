"""Standard API envelope: {success, data, meta} / {success, error}."""
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.core.logging import get_logger

log = get_logger(__name__)


def ok(data: Any = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"success": True, "data": data, "meta": meta or {}}


def error_body(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, "details": details or {}}}


def _with_request_id(request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    rid = getattr(request.state, "request_id", None)
    if rid:
        body.setdefault("meta", {})["request_id"] = rid
    return body


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content=_with_request_id(request, error_body(exc.code, exc.message, exc.details)),
                        headers=exc.headers)


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_map = {401: "AUTH_REQUIRED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
                405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE", 429: "RATE_LIMIT_EXCEEDED"}
    code = code_map.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(status_code=exc.status_code,
                        content=_with_request_id(request, error_body(code, str(exc.detail))),
                        headers=getattr(exc, "headers", None))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = {"errors": [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
                          for e in exc.errors()]}
    return JSONResponse(status_code=422,
                        content=_with_request_id(request, error_body("VALIDATION_ERROR",
                                                                     "Request validation failed", details)))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", extra={"path": request.url.path})
    return JSONResponse(status_code=500,
                        content=_with_request_id(request, error_body("INTERNAL_ERROR", "Internal server error")))
