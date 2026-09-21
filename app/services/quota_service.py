"""Quota enforcement on top of usage_service. Reservations are atomic; callers must
release on failure so a failed upload/search never consumes quota."""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SearchLimitExceeded, StorageLimitExceeded, UploadLimitExceeded
from app.models import Package
from app.services import usage_service


class QuotaReservation:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self._held: list[tuple[str, int]] = []

    async def reserve_upload(self, pkg: Package) -> None:
        if not await usage_service.reserve(self.tenant_id, "upload", pkg.upload_limit):
            raise UploadLimitExceeded(details={"limit": pkg.upload_limit})
        self._held.append(("upload", 1))

    async def reserve_search(self, pkg: Package) -> None:
        if not await usage_service.reserve(self.tenant_id, "search", pkg.search_limit):
            raise SearchLimitExceeded(details={"limit": pkg.search_limit})
        self._held.append(("search", 1))

    async def reserve_storage(self, pkg: Package, size_bytes: int) -> None:
        if not await usage_service.reserve(self.tenant_id, "storage", pkg.storage_limit, size_bytes):
            raise StorageLimitExceeded(details={"limit_bytes": pkg.storage_limit, "requested_bytes": size_bytes})
        self._held.append(("storage", size_bytes))

    async def release_all(self) -> None:
        for kind, qty in self._held:
            await usage_service.release(self.tenant_id, kind, qty)
        self._held.clear()

    def commit(self) -> None:
        """Reservations become permanent — nothing to undo."""
        self._held.clear()


async def release_storage(db: AsyncSession, tenant_id: uuid.UUID, size_bytes: int) -> None:
    await usage_service.release(tenant_id, "storage", size_bytes)
