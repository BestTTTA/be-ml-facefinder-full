"""Private object storage with signed URLs.

Layout: tenant/{tenant_id}/persons/{person_id}/original/{image_id}.{ext}
                                           /thumbnails/{image_id}.jpg
Backends: MinIO/S3 (existing infra) or Supabase Storage. Credentials never leave this module.
"""
import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Optional, Protocol

import httpx
from PIL import Image

from app.core.config import Settings, get_settings
from app.core.exceptions import StorageFailed
from app.core.logging import get_logger

log = get_logger(__name__)
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def signed_url(self, key: str, ttl_seconds: int) -> str: ...
    async def ping(self) -> bool: ...


class MinioBackend:
    def __init__(self, s: Settings):
        from minio import Minio
        if not (s.MINIO_ENDPOINT and s.MINIO_ACCESS_KEY and s.MINIO_SECRET_KEY):
            raise RuntimeError("MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY are required for STORAGE_BACKEND=minio")
        self.client = Minio(s.MINIO_ENDPOINT, access_key=s.MINIO_ACCESS_KEY, secret_key=s.MINIO_SECRET_KEY,
                            secure=s.MINIO_SSL, region=s.MINIO_REGION)
        # Presigned URLs embed the host in the signature, so they must be signed for the address
        # clients will actually use (MINIO_PUBLIC_URL), not the in-network endpoint.
        self.sign_client = self.client
        if s.MINIO_PUBLIC_URL:
            from urllib.parse import urlparse
            u = urlparse(s.MINIO_PUBLIC_URL if "://" in s.MINIO_PUBLIC_URL else f"http://{s.MINIO_PUBLIC_URL}")
            # fixed region: presigning must not perform a bucket-location lookup against the public host
            self.sign_client = Minio(u.netloc, access_key=s.MINIO_ACCESS_KEY, secret_key=s.MINIO_SECRET_KEY,
                                     secure=(u.scheme == "https"), region=s.MINIO_REGION)
        self.bucket = s.STORAGE_BUCKET
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="storage")
        self._bucket_checked = False

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def _ensure_bucket(self) -> None:
        if not self._bucket_checked:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)  # private by default (no anonymous policy)
            self._bucket_checked = True

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        def _do():
            self._ensure_bucket()
            self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        await self._run(_do)

    async def delete(self, key: str) -> None:
        await self._run(lambda: self.client.remove_object(self.bucket, key))

    async def signed_url(self, key: str, ttl_seconds: int) -> str:
        return await self._run(lambda: self.sign_client.presigned_get_object(self.bucket, key, expires=timedelta(seconds=ttl_seconds)))

    async def ping(self) -> bool:
        try:
            await self._run(lambda: self.client.bucket_exists(self.bucket))
            return True
        except Exception:
            return False


class SupabaseStorageBackend:
    def __init__(self, s: Settings):
        if not (s.SUPABASE_URL and s.SUPABASE_SERVICE_ROLE_KEY):
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are required for STORAGE_BACKEND=supabase")
        self.base = s.SUPABASE_URL.rstrip("/") + "/storage/v1"
        self.bucket = s.STORAGE_BUCKET
        self._headers = {"Authorization": f"Bearer {s.SUPABASE_SERVICE_ROLE_KEY}", "apikey": s.SUPABASE_SERVICE_ROLE_KEY}
        self._client = httpx.AsyncClient(timeout=30)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        r = await self._client.post(f"{self.base}/object/{self.bucket}/{key}", content=data,
                                    headers={**self._headers, "Content-Type": content_type, "x-upsert": "true"})
        if r.status_code >= 400:
            raise StorageFailed(details={"status": r.status_code})

    async def delete(self, key: str) -> None:
        await self._client.request("DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": [key]}, headers=self._headers)

    async def signed_url(self, key: str, ttl_seconds: int) -> str:
        r = await self._client.post(f"{self.base}/object/sign/{self.bucket}/{key}", json={"expiresIn": ttl_seconds}, headers=self._headers)
        if r.status_code >= 400:
            raise StorageFailed(details={"status": r.status_code})
        return self.base + r.json()["signedURL"]

    async def ping(self) -> bool:
        try:
            r = await self._client.get(f"{self.base}/bucket/{self.bucket}", headers=self._headers)
            return r.status_code == 200
        except Exception:
            return False


class MemoryBackend:
    """In-process backend for tests."""
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def signed_url(self, key: str, ttl_seconds: int) -> str:
        return f"memory://{key}?ttl={ttl_seconds}"

    async def ping(self) -> bool:
        return True


class StorageService:
    def __init__(self, backend: StorageBackend, settings: Optional[Settings] = None):
        self.backend = backend
        self.settings = settings or get_settings()

    @staticmethod
    def original_key(tenant_id, person_id, image_id, mime_type: str) -> str:
        return f"tenant/{tenant_id}/persons/{person_id}/original/{image_id}.{_EXT.get(mime_type, 'bin')}"

    @staticmethod
    def thumbnail_key(tenant_id, person_id, image_id) -> str:
        return f"tenant/{tenant_id}/persons/{person_id}/thumbnails/{image_id}.jpg"

    @staticmethod
    def make_thumbnail(data: bytes, size: int = 256) -> bytes:
        with Image.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB")
            rgb.thumbnail((size, size))
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

    async def store_image(self, tenant_id, person_id, image_id, data: bytes, mime_type: str,
                          thumbnail: Optional[bytes] = None) -> tuple[str, str]:
        okey = self.original_key(tenant_id, person_id, image_id, mime_type)
        tkey = self.thumbnail_key(tenant_id, person_id, image_id)
        try:
            await self.backend.put(okey, data, mime_type)
            await self.backend.put(tkey, thumbnail if thumbnail is not None else self.make_thumbnail(data), "image/jpeg")
        except StorageFailed:
            await self._cleanup(okey, tkey)
            raise
        except Exception as e:
            log.exception("storage_put_failed")
            await self._cleanup(okey, tkey)
            raise StorageFailed(details={"reason": type(e).__name__}) from e
        return okey, tkey

    async def _cleanup(self, *keys: str) -> None:
        for k in keys:
            try:
                await self.backend.delete(k)
            except Exception:
                pass

    async def delete_keys(self, *keys: Optional[str]) -> None:
        await self._cleanup(*[k for k in keys if k])

    async def signed_url(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        try:
            return await self.backend.signed_url(key, self.settings.SIGNED_URL_TTL_SECONDS)
        except Exception:
            log.warning("signed_url_failed", extra={"key": key})
            return None

    async def ping(self) -> bool:
        return await self.backend.ping()


_service: Optional[StorageService] = None


def get_storage_service() -> StorageService:
    global _service
    if _service is None:
        s = get_settings()
        backend = SupabaseStorageBackend(s) if s.STORAGE_BACKEND == "supabase" else MinioBackend(s)
        _service = StorageService(backend, s)
    return _service


def set_storage_service(service: Optional[StorageService]) -> None:
    global _service
    _service = service
