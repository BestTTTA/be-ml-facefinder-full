"""Liveness / readiness probes. Mounted at /health (no version prefix, no auth)."""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_engine
from app.core.redis import get_redis
from app.services.embedding_service import get_embedding_service
from app.services.storage_service import get_storage_service
from app.services.supabase_service import get_supabase_service

router = APIRouter(prefix="/health", tags=["health"])


async def _check(name: str, coro) -> Dict[str, Any]:
    try:
        okv = await asyncio.wait_for(coro, timeout=5)
        return {"name": name, "ok": bool(okv)}
    except Exception as e:
        return {"name": name, "ok": False, "error": type(e).__name__}


async def _db() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def _redis() -> bool:
    return bool(await get_redis().ping())


async def _storage() -> bool:
    return await get_storage_service().ping()


async def _supabase() -> bool:
    return await get_supabase_service().ping()


async def _engine() -> bool:
    return get_embedding_service().is_ready()


@router.get("", summary="Full health report")
async def health(response: Response):
    s = get_settings()
    checks = await asyncio.gather(_check("api", asyncio.sleep(0, result=True)), _check("database", _db()),
                                  _check("redis", _redis()), _check("storage", _storage()),
                                  _check("supabase", _supabase()), _check("face_engine", _engine()))
    critical = {"api", "database", "redis", "face_engine"}
    healthy = all(c["ok"] for c in checks if c["name"] in critical)
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", "version": s.APP_VERSION, "env": s.APP_ENV,
            "checks": {c["name"]: c for c in checks}}


@router.get("/live", summary="Liveness")
async def live():
    return {"status": "ok"}


@router.get("/ready", summary="Readiness (DB, Redis, face engine)")
async def ready(response: Response):
    checks = await asyncio.gather(_check("database", _db()), _check("redis", _redis()), _check("face_engine", _engine()))
    healthy = all(c["ok"] for c in checks)
    response.status_code = 200 if healthy else 503
    return {"status": "ready" if healthy else "not_ready", "checks": {c["name"]: c for c in checks}}
