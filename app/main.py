"""Face Recognition API — application factory.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Docs: /api/docs  ·  OpenAPI: /api/openapi.json  ·  Health: /health
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import health
from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.database import dispose_engine, get_sessionmaker
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.core.responses import app_error_handler, http_error_handler, unhandled_error_handler, validation_error_handler
from app.middleware.request_context import RequestContextMiddleware
from app.services.admin_service import bootstrap_admin
from app.services.embedding_service import get_embedding_service

log = get_logger(__name__)

DESCRIPTION = """
Commercial-grade Face Recognition API.

**Authentication**
* End users: Supabase Auth (Google OAuth) → send `Authorization: Bearer <access_token>`.
* Integrations: API keys → `X-API-Key: fr_live_...` (scoped: `faces:upload`, `faces:search`, `faces:read`, `persons:read`).
* Admins: `POST /api/v1/admin/auth/login` → `Authorization: Bearer <admin token>`.

**Responses** always use `{"success": true, "data": ..., "meta": ...}` or
`{"success": false, "error": {"code", "message", "details"}}`.

**Limits** come from the account's package: monthly upload/search quotas, storage, and per-minute rate limit
(`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`). Exceeding either returns `429`.

**Tenancy**: every person/face/image belongs to the caller's tenant; cross-tenant access is impossible.
"""

TAGS = [
    {"name": "auth", "description": "Supabase session refresh / logout"},
    {"name": "me", "description": "Current user, package, quota and usage"},
    {"name": "api-keys", "description": "Create, list, revoke and rotate API keys (session login required)"},
    {"name": "faces", "description": "Register faces, search faces, manage persons"},
    {"name": "packages", "description": "Public package catalogue"},
    {"name": "admin", "description": "Administration (separate auth, RBAC, audited)"},
    {"name": "health", "description": "Probes"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.LOG_LEVEL)
    if s.is_production and s.ADMIN_JWT_SECRET == "change-me-in-production":
        raise RuntimeError("ADMIN_JWT_SECRET must be set in production")
    # Load the face model once per worker process.
    get_embedding_service().load()
    async with get_sessionmaker()() as db:
        await bootstrap_admin(db)
        await db.commit()
    log.info("startup_complete", extra={"env": s.APP_ENV, "engine": s.FACE_ENGINE, "storage": s.STORAGE_BACKEND})
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.APP_NAME, version=s.APP_VERSION, description=DESCRIPTION, openapi_tags=TAGS,
                  docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list,
                       allow_credentials=s.cors_origin_list != ["*"], allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["X-Request-ID", "X-RateLimit-Limit",
                                                            "X-RateLimit-Remaining", "X-RateLimit-Reset"])

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(health.router)
    app.include_router(v1_router)

    if s.LEGACY_API_ENABLED:  # pre-SaaS Facemenow event API, kept for backward compatibility
        from main import app as legacy_app  # noqa: WPS433
        app.mount("/legacy", legacy_app)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"name": s.APP_NAME, "version": s.APP_VERSION, "docs": "/api/docs", "health": "/health"}

    return app


app = create_app()
