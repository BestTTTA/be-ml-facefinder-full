"""Request ID propagation, structured access logging, and security headers."""
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import get_settings
from app.core.logging import api_key_id_ctx, get_logger, request_id_ctx, user_id_ctx

log = get_logger("access")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Permitted-Cross-Domain-Policies": "none",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        rid = rid[:64]
        request.state.request_id = rid
        t_rid = request_id_ctx.set(rid)
        t_uid = user_id_ctx.set(None)
        t_kid = api_key_id_ctx.set(None)
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            rl = getattr(request.state, "rate_limit", None)
            if rl is not None:
                for k, v in rl.headers().items():
                    response.headers[k] = v
            for k, v in _SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
            if get_settings().is_production:
                response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
            return response
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            if not request.url.path.startswith("/health"):
                log.info("request", extra={"method": request.method, "endpoint": request.url.path,
                                           "status_code": status, "processing_time_ms": ms,
                                           "user_id": user_id_ctx.get(), "api_key_id": api_key_id_ctx.get(),
                                           "ip": request.client.host if request.client else None})
            request_id_ctx.reset(t_rid)
            user_id_ctx.reset(t_uid)
            api_key_id_ctx.reset(t_kid)
