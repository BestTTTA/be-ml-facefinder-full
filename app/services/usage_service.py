"""Usage periods (one row per tenant per calendar month, never deleted) and immutable usage logs."""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_sessionmaker
from app.core.redis import get_redis
from app.models import Package, Subscription, UsageLog, UsagePeriod

UNLIMITED = -1


def current_period_bounds(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


async def get_or_create_period(db: AsyncSession, tenant_id: uuid.UUID) -> UsagePeriod:
    """Idempotent + concurrency-safe: INSERT ... ON CONFLICT DO NOTHING, then SELECT.
    A new month's row inherits storage_used from the previous period (storage is not reset)."""
    start, end = current_period_bounds()
    prev_storage = (await db.execute(
        select(UsagePeriod.storage_used).where(UsagePeriod.tenant_id == tenant_id, UsagePeriod.period_start < start)
        .order_by(UsagePeriod.period_start.desc()).limit(1))).scalar_one_or_none() or 0
    await db.execute(pg_insert(UsagePeriod).values(id=uuid.uuid4(), tenant_id=tenant_id, period_start=start,
                                                  period_end=end, storage_used=prev_storage)
                     .on_conflict_do_nothing(constraint="ux_usage_periods_tenant_start"))
    return (await db.execute(select(UsagePeriod).where(UsagePeriod.tenant_id == tenant_id,
                                                        UsagePeriod.period_start == start))).scalar_one()


_COUNTER = {"upload": "upload_count", "search": "search_count", "storage": "storage_used"}


async def reserve(tenant_id: uuid.UUID, kind: str, limit: int, quantity: int = 1) -> bool:
    """Atomically add `quantity` to the period counter iff it stays within `limit`.
    Returns False when the quota would be exceeded. Safe under concurrent requests
    because the check and the increment happen in a single UPDATE, committed in its
    own short transaction (so no row lock is held while the model runs)."""
    col = _COUNTER[kind]
    cond = "" if limit == UNLIMITED else f" AND {col} + :q <= :limit"
    async with get_sessionmaker()() as db:
        period = await get_or_create_period(db, tenant_id)
        res = await db.execute(text(f"UPDATE usage_periods SET {col} = {col} + :q, updated_at = now() "
                                    f"WHERE id = :id{cond} RETURNING id"),
                               {"q": quantity, "limit": limit, "id": period.id})
        ok = res.scalar_one_or_none() is not None
        await db.commit()
    return ok


async def release(tenant_id: uuid.UUID, kind: str, quantity: int = 1) -> None:
    """Undo a reservation after a failed operation (never below zero)."""
    col = _COUNTER[kind]
    async with get_sessionmaker()() as db:
        period = await get_or_create_period(db, tenant_id)
        await db.execute(text(f"UPDATE usage_periods SET {col} = GREATEST({col} - :q, 0), updated_at = now() WHERE id = :id"),
                         {"q": quantity, "id": period.id})
        await db.commit()


async def log_usage(db: AsyncSession, tenant_id: uuid.UUID, kind: str, *, user_id=None, api_key_id=None,
                    resource_id=None, quantity: int = 1, idempotency_key: Optional[str] = None,
                    request_id: Optional[str] = None) -> None:
    stmt = pg_insert(UsageLog).values(id=uuid.uuid4(), tenant_id=tenant_id, user_id=user_id, api_key_id=api_key_id,
                                      kind=kind, quantity=quantity, resource_id=resource_id,
                                      idempotency_key=idempotency_key, request_id=request_id)
    await db.execute(stmt.on_conflict_do_nothing(constraint="ux_usage_logs_tenant_idem"))


# --- API request counting (Redis, cheap) ------------------------------------

def _api_count_key(tenant_id: uuid.UUID) -> str:
    start, _ = current_period_bounds()
    return f"usage:api:{tenant_id}:{start:%Y%m}"


async def record_api_request(tenant_id: uuid.UUID) -> None:
    r = get_redis()
    key = _api_count_key(tenant_id)
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 45 * 86400)
    await pipe.execute()


async def _sync_api_count(db: AsyncSession, period: UsagePeriod) -> int:
    try:
        n = int(await get_redis().get(_api_count_key(period.tenant_id)) or 0)
    except Exception:
        n = 0
    if n > period.api_request_count:
        period.api_request_count = n
    return period.api_request_count


# --- reporting ---------------------------------------------------------------

def _entry(used: int, limit: int) -> Dict[str, Any]:
    return {"used": used, "limit": None if limit == UNLIMITED else limit,
            "remaining": None if limit == UNLIMITED else max(limit - used, 0), "unlimited": limit == UNLIMITED}


async def usage_summary(db: AsyncSession, tenant_id: uuid.UUID, sub: Subscription) -> Dict[str, Any]:
    period = await get_or_create_period(db, tenant_id)
    pkg: Package = sub.package
    api_count = await _sync_api_count(db, period)
    return {
        "package": {"id": str(pkg.id), "name": pkg.name},
        "subscription": {"status": sub.status, "started_at": sub.started_at, "expired_at": sub.expired_at},
        "period": {"start": period.period_start, "end": period.period_end},
        "uploads": _entry(period.upload_count, pkg.upload_limit),
        "searches": _entry(period.search_count, pkg.search_limit),
        "storage": _entry(period.storage_used, pkg.storage_limit),
        "api_requests": {"used": api_count, "rate_limit_per_minute": pkg.api_rate_limit},
    }


async def usage_history(db: AsyncSession, tenant_id: uuid.UUID, limit: int = 12) -> List[UsagePeriod]:
    q = (select(UsagePeriod).where(UsagePeriod.tenant_id == tenant_id)
         .order_by(UsagePeriod.period_start.desc()).limit(limit))
    return list((await db.execute(q)).scalars())


async def totals_today(db: AsyncSession) -> Dict[str, int]:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (await db.execute(select(UsageLog.kind, func.coalesce(func.sum(UsageLog.quantity), 0))
                             .where(UsageLog.created_at >= start, UsageLog.kind.in_(["upload", "search"]))
                             .group_by(UsageLog.kind))).all()
    out = {"upload": 0, "search": 0}
    for kind, n in rows:
        out[kind] = int(n)
    return out
