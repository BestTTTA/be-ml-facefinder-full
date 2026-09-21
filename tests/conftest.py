"""Integration test harness.

Real PostgreSQL + pgvector (docker compose `postgres`, database `facefinder_test`),
fakeredis, the deterministic `fake` face engine, and in-memory object storage.
Set TEST_DATABASE_URL to point elsewhere.
"""
import asyncio
import io
import os
import time
import uuid
from typing import AsyncIterator, Dict, Optional

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("FACE_ENGINE", "fake")
os.environ.setdefault("STORAGE_BACKEND", "minio")
os.environ.setdefault("MINIO_ENDPOINT", "unused:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "unused")
os.environ.setdefault("MINIO_SECRET_KEY", "unused")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-for-unit-tests-only")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("ADMIN_JWT_SECRET", "test-admin-secret")
os.environ.setdefault("ADMIN_BOOTSTRAP_USERNAME", "superadmin")
os.environ.setdefault("ADMIN_BOOTSTRAP_PASSWORD", "super-secret-password-123")
os.environ.setdefault("FACE_SIMILARITY_THRESHOLD", "0.6")
os.environ.setdefault("RATE_LIMIT_ENABLED", "true")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://facefinder:facefinder@localhost:5555/facefinder_test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import asyncpg  # noqa: E402
import fakeredis.aioredis  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core import redis as redis_mod  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine  # noqa: E402
from app.services.storage_service import MemoryBackend, StorageService, set_storage_service  # noqa: E402

SEED_TABLES = {"alembic_version", "packages", "roles", "permissions", "role_permissions"}


async def _create_database() -> None:
    url = os.environ["DATABASE_URL"]
    base, dbname = url.rsplit("/", 1)
    dsn = (base + "/postgres").replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", dbname):
            await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


def _ensure_database() -> None:
    asyncio.run(_create_database())
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _database():
    _ensure_database()
    yield


@pytest.fixture(scope="session")
def storage_backend() -> MemoryBackend:
    backend = MemoryBackend()
    set_storage_service(StorageService(backend, get_settings()))
    return backend


@pytest.fixture(scope="session")
async def app(_database, storage_backend):
    from app.main import app as _app
    redis_mod.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    async with _app.router.lifespan_context(_app):
        yield _app


@pytest.fixture(autouse=True)
async def _clean_tables(app, storage_backend):
    """Truncate all non-seed tables before every test; reset Redis."""
    engine = get_engine()
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).scalars()
        tables = [t for t in rows if t not in SEED_TABLES]
        await conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE"))
        await conn.execute(text("UPDATE packages SET is_active = true"))
    await redis_mod.get_redis().flushall()
    storage_backend.objects.clear()
    # re-bootstrap admin (truncated)
    from app.core.database import get_sessionmaker
    from app.services.admin_service import bootstrap_admin
    async with get_sessionmaker()() as db:
        await bootstrap_admin(db)
        await db.commit()
    yield


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --- helpers -------------------------------------------------------------------

def make_jwt(user_id: Optional[str] = None, email: str = "user@example.com", name: str = "Test User",
             exp_delta: int = 3600, secret: Optional[str] = None, aud: str = "authenticated") -> str:
    now = int(time.time())
    claims = {"sub": user_id or str(uuid.uuid4()), "aud": aud, "email": email, "role": "authenticated",
              "iat": now, "exp": now + exp_delta,
              "user_metadata": {"full_name": name, "avatar_url": "https://example.com/a.png", "email": email}}
    return jwt.encode(claims, secret or os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")


def auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_image(color=(200, 30, 30), width: int = 100, height: int = 100, fmt: str = "JPEG") -> bytes:
    im = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


def img_file(data: bytes, name: str = "face.jpg", mime: str = "image/jpeg"):
    return {"image": (name, data, mime)}


@pytest.fixture
def user_token() -> str:
    return make_jwt()


@pytest.fixture
async def admin_token(client) -> str:
    r = await client.post("/api/v1/admin/auth/login",
                          json={"username": "superadmin", "password": "super-secret-password-123"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


async def set_package_limits(client, admin_token, name: str, **limits) -> None:
    r = await client.get("/api/v1/admin/packages", headers=auth(admin_token))
    pkg = next(p for p in r.json()["data"] if p["name"] == name)
    r = await client.patch(f"/api/v1/admin/packages/{pkg['id']}", headers=auth(admin_token), json=limits)
    assert r.status_code == 200, r.text
