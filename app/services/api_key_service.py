"""API key lifecycle. Raw keys are returned exactly once; only the SHA-256 hash is stored."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationFailed
from app.core.permissions import ALL_SCOPES
from app.core.security import generate_api_key
from app.models import ApiKey

EXPIRY_PRESETS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "1y": 365}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_expiry(expires_in: Optional[str], expires_at: Optional[datetime]) -> Optional[datetime]:
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _now():
            raise ValidationFailed("expires_at must be in the future")
        return expires_at
    if expires_in in (None, "", "never"):
        return None
    if expires_in not in EXPIRY_PRESETS:
        raise ValidationFailed("expires_in must be one of " + ", ".join(EXPIRY_PRESETS) + " or 'never'")
    return _now() + timedelta(days=EXPIRY_PRESETS[expires_in])


def validate_scopes(scopes: List[str]) -> List[str]:
    bad = sorted(set(scopes) - ALL_SCOPES)
    if bad:
        raise ValidationFailed("Unknown scopes", details={"invalid": bad, "allowed": sorted(ALL_SCOPES)})
    return sorted(set(scopes)) or sorted(ALL_SCOPES)


async def create_key(db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, name: str,
                     scopes: List[str], expires_at: Optional[datetime],
                     rotated_from: Optional[uuid.UUID] = None) -> Tuple[ApiKey, str]:
    raw, prefix, key_hash = generate_api_key()
    key = ApiKey(tenant_id=tenant_id, user_id=user_id, name=name, key_prefix=prefix, key_hash=key_hash,
                 scopes=validate_scopes(scopes), status="active", expires_at=expires_at, rotated_from_id=rotated_from)
    db.add(key)
    await db.flush()
    return key, raw


async def list_keys(db: AsyncSession, user_id: uuid.UUID) -> List[ApiKey]:
    q = select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    keys = list((await db.execute(q)).scalars())
    now = _now()
    for k in keys:  # surface expiry lazily
        if k.status == "active" and k.expires_at and k.expires_at < now:
            k.status = "expired"
    return keys


async def get_owned_key(db: AsyncSession, user_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user_id:  # never reveal other users' key ids
        raise NotFound("API key not found")
    return key


async def revoke_key(db: AsyncSession, key: ApiKey) -> ApiKey:
    if key.status != "revoked":
        key.status = "revoked"
        key.revoked_at = _now()
    await db.flush()
    return key


async def rotate_key(db: AsyncSession, key: ApiKey) -> Tuple[ApiKey, str]:
    """Revokes the old key and issues a new one with the same name/scopes/expiry."""
    await revoke_key(db, key)
    return await create_key(db, tenant_id=key.tenant_id, user_id=key.user_id, name=key.name,
                            scopes=list(key.scopes or []), expires_at=key.expires_at, rotated_from=key.id)
