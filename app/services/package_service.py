"""Packages (DB-driven limits) and subscriptions (state only; billing lives elsewhere)."""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Conflict, PackageExpired, PackageNotFound, SubscriptionInactive
from app.models import Package, Subscription

ACTIVE_STATES = {"trial", "active"}
UNLIMITED = -1


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- packages ---------------------------------------------------------------

async def list_packages(db: AsyncSession, include_inactive: bool = False) -> List[Package]:
    q = select(Package).order_by(Package.price, Package.name)
    if not include_inactive:
        q = q.where(Package.is_active.is_(True))
    return list((await db.execute(q)).scalars())


async def get_package(db: AsyncSession, package_id: uuid.UUID) -> Package:
    pkg = await db.get(Package, package_id)
    if not pkg:
        raise PackageNotFound()
    return pkg


async def get_default_package(db: AsyncSession) -> Package:
    pkg = (await db.execute(select(Package).where(Package.is_default.is_(True), Package.is_active.is_(True)))).scalar_one_or_none()
    if pkg is None:
        name = get_settings().DEFAULT_PACKAGE_NAME
        pkg = (await db.execute(select(Package).where(Package.name == name))).scalar_one_or_none()
    if pkg is None:
        raise PackageNotFound("No default package configured")
    return pkg


async def create_package(db: AsyncSession, data: Dict[str, Any]) -> Package:
    if (await db.execute(select(Package.id).where(Package.name == data["name"]))).scalar_one_or_none():
        raise Conflict("Package name already exists")
    pkg = Package(**data)
    if pkg.is_default:
        await db.execute(update(Package).values(is_default=False))
    db.add(pkg)
    await db.flush()
    return pkg


async def update_package(db: AsyncSession, package_id: uuid.UUID, data: Dict[str, Any]) -> Package:
    pkg = await get_package(db, package_id)
    if "name" in data and data["name"] != pkg.name:
        if (await db.execute(select(Package.id).where(Package.name == data["name"]))).scalar_one_or_none():
            raise Conflict("Package name already exists")
    if data.get("is_default"):
        await db.execute(update(Package).where(Package.id != package_id).values(is_default=False))
    for k, v in data.items():
        setattr(pkg, k, v)
    await db.flush()
    return pkg


async def delete_package(db: AsyncSession, package_id: uuid.UUID) -> str:
    """Hard-deletes a package nobody ever subscribed to; otherwise deactivates it so
    historical subscriptions/usage reports stay intact. Returns "deleted" | "deactivated"."""
    pkg = await get_package(db, package_id)
    in_use = (await db.execute(select(func.count()).select_from(Subscription)
                               .where(Subscription.package_id == package_id,
                                      Subscription.status.in_(ACTIVE_STATES)))).scalar_one()
    if in_use:
        raise Conflict("Package has active subscriptions; move them first", details={"active_subscriptions": in_use})
    if pkg.is_default:
        raise Conflict("Cannot delete the default package")
    referenced = (await db.execute(select(func.count()).select_from(Subscription)
                                   .where(Subscription.package_id == package_id))).scalar_one()
    if referenced:
        pkg.is_active = False
        await db.flush()
        return "deactivated"
    await db.delete(pkg)
    await db.flush()
    return "deleted"


# --- subscriptions ----------------------------------------------------------

async def get_current_subscription(db: AsyncSession, tenant_id: uuid.UUID) -> Optional[Subscription]:
    q = (select(Subscription).where(Subscription.tenant_id == tenant_id)
         .order_by(Subscription.created_at.desc()).limit(1))
    return (await db.execute(q)).scalar_one_or_none()


async def ensure_subscription(db: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    sub = await get_current_subscription(db, tenant_id)
    if sub is None:
        pkg = await get_default_package(db)
        sub = Subscription(tenant_id=tenant_id, package_id=pkg.id, status="active", started_at=now())
        db.add(sub)
        await db.flush()
        await db.refresh(sub, attribute_names=["package"])
    return sub


def assert_subscription_usable(sub: Subscription) -> None:
    if sub.expired_at and sub.expired_at < now() and sub.status in ACTIVE_STATES:
        raise PackageExpired(details={"expired_at": sub.expired_at.isoformat()})
    if sub.status == "expired":
        raise PackageExpired()
    if sub.status not in ACTIVE_STATES:
        raise SubscriptionInactive(details={"status": sub.status})
    if not sub.package.is_active:
        raise SubscriptionInactive("Package is no longer available", details={"package": sub.package.name})


async def change_package(db: AsyncSession, tenant_id: uuid.UUID, package_id: uuid.UUID,
                         status: str = "active", expired_at: Optional[datetime] = None) -> Subscription:
    """Ends the current subscription and starts a new one (history preserved)."""
    pkg = await get_package(db, package_id)
    current = await get_current_subscription(db, tenant_id)
    if current and current.status in ACTIVE_STATES:
        current.status = "cancelled"
        current.cancelled_at = now()
    sub = Subscription(tenant_id=tenant_id, package_id=pkg.id, status=status, started_at=now(), expired_at=expired_at)
    db.add(sub)
    await db.flush()
    await db.refresh(sub, attribute_names=["package"])
    return sub


async def set_subscription_status(db: AsyncSession, tenant_id: uuid.UUID, status: str) -> Subscription:
    sub = await ensure_subscription(db, tenant_id)
    sub.status = status
    if status == "cancelled":
        sub.cancelled_at = now()
    await db.flush()
    return sub
