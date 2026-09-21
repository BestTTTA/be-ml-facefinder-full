"""Resolves the request principal (Supabase JWT user or API key) and enforces account status."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidApiKey, InvalidToken, TokenExpired, TokenRevoked, UserNotFound, UserSuspended
from app.core.security import hash_api_key
from app.models import ApiKey, Profile, Subscription, Tenant
from app.services import package_service
from app.services.supabase_service import SupabaseService, get_supabase_service


@dataclass
class Principal:
    profile: Profile
    tenant: Tenant
    subscription: Subscription
    auth_type: str                     # "jwt" | "api_key"
    api_key: Optional[ApiKey] = None
    scopes: List[str] = field(default_factory=list)
    claims: Dict[str, Any] = field(default_factory=dict)

    @property
    def user_id(self) -> uuid.UUID:
        return self.profile.id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def package(self):
        return self.subscription.package

    @property
    def api_key_id(self) -> Optional[uuid.UUID]:
        return self.api_key.id if self.api_key else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_active(profile: Profile) -> None:
    if profile.status == "deleted" or profile.deleted_at:
        raise UserNotFound()
    if profile.status == "suspended" or profile.tenant.status == "suspended":
        raise UserSuspended()


async def get_or_create_profile(db: AsyncSession, claims: Dict[str, Any]) -> Profile:
    """First login provisions tenant + profile + default subscription. Later logins sync name/avatar."""
    user_id = uuid.UUID(claims["sub"])
    meta = claims.get("user_metadata") or {}
    email = claims.get("email") or meta.get("email") or ""
    name = meta.get("full_name") or meta.get("name")
    avatar = meta.get("avatar_url") or meta.get("picture")

    profile = await db.get(Profile, user_id)
    if profile is None:
        try:
            tenant = Tenant(name=name or email or str(user_id), owner_id=user_id)
            db.add(tenant)
            await db.flush()
            profile = Profile(id=user_id, tenant_id=tenant.id, email=email, name=name, avatar_url=avatar,
                              status="active", role="owner", last_login_at=_now())
            db.add(profile)
            await db.flush()
            await db.refresh(profile, attribute_names=["tenant"])
            await package_service.ensure_subscription(db, tenant.id)
            # Provisioning must be visible to the short-lived quota/usage transactions of this same request.
            await db.commit()
        except IntegrityError:
            # Concurrent first logins of the same user: another request provisioned it — use that row.
            await db.rollback()
            profile = await db.get(Profile, user_id)
            if profile is None:
                raise
    else:
        changed = False
        for attr, val in (("email", email), ("name", name), ("avatar_url", avatar)):
            if val and getattr(profile, attr) != val:
                setattr(profile, attr, val)
                changed = True
        if changed or not profile.last_login_at or (_now() - profile.last_login_at) > timedelta(minutes=5):
            profile.last_login_at = _now()
    return profile


async def authenticate_jwt(db: AsyncSession, token: str, supabase: Optional[SupabaseService] = None) -> Principal:
    claims = (supabase or get_supabase_service()).verify_access_token(token)
    profile = await get_or_create_profile(db, claims)
    _assert_active(profile)
    sub = await package_service.ensure_subscription(db, profile.tenant_id)
    return Principal(profile=profile, tenant=profile.tenant, subscription=sub, auth_type="jwt", claims=claims)


async def authenticate_api_key(db: AsyncSession, raw_key: str) -> Principal:
    key = (await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key)))).scalar_one_or_none()
    if key is None:
        raise InvalidApiKey()
    if key.status == "revoked" or key.revoked_at:
        raise TokenRevoked("API key has been revoked")
    if key.expires_at and key.expires_at < _now():
        if key.status != "expired":
            key.status = "expired"
        raise TokenExpired("API key has expired")
    profile = await db.get(Profile, key.user_id)
    if profile is None:
        raise InvalidApiKey()
    _assert_active(profile)
    if not key.last_used_at or (_now() - key.last_used_at) > timedelta(minutes=1):
        key.last_used_at = _now()
    sub = await package_service.ensure_subscription(db, key.tenant_id)
    return Principal(profile=profile, tenant=profile.tenant, subscription=sub, auth_type="api_key",
                     api_key=key, scopes=list(key.scopes or []))


async def load_profile(db: AsyncSession, user_id: uuid.UUID) -> Profile:
    profile = await db.get(Profile, user_id)
    if profile is None:
        raise UserNotFound()
    return profile


def verify_supabase_token_only(token: str) -> Dict[str, Any]:
    try:
        return get_supabase_service().verify_access_token(token)
    except (TokenExpired, InvalidToken):
        raise
