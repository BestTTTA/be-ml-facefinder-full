"""Admin auth (username/password → Argon2id → server-side session + HS256 token) and admin operations."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import Conflict, InvalidToken, PermissionDenied, TokenRevoked, UserNotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.permissions import ALL_ROLES
from app.core.security import create_admin_token, decode_admin_token, hash_password, verify_password
from app.models import (
    AdminSession,
    AdminUser,
    Face,
    Image,
    Package,
    Person,
    Profile,
    Role,
    Subscription,
    UsagePeriod,
    UserRole,
)

log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_DUMMY_HASH = hash_password("dummy-password-for-timing")  # keeps login timing uniform for unknown users


@dataclass
class AdminPrincipal:
    admin: AdminUser
    session: AdminSession
    permissions: Set[str]
    roles: List[str]

    def has(self, perm: str) -> bool:
        return perm in self.permissions

    def require(self, perm: str) -> None:
        if perm not in self.permissions:
            raise PermissionDenied(details={"required_permission": perm})


def permissions_for(admin: AdminUser) -> Set[str]:
    return {p.name for r in admin.roles for p in r.permissions}


# --- bootstrap / accounts -----------------------------------------------------

async def bootstrap_admin(db: AsyncSession) -> Optional[AdminUser]:
    """Creates the first SUPER_ADMIN from env vars if no admin exists. Never logs the password."""
    s = get_settings()
    if not (s.ADMIN_BOOTSTRAP_USERNAME and s.ADMIN_BOOTSTRAP_PASSWORD):
        return None
    existing = (await db.execute(select(func.count()).select_from(AdminUser))).scalar_one()
    if existing:
        return None
    admin = await create_admin(db, username=s.ADMIN_BOOTSTRAP_USERNAME, password=s.ADMIN_BOOTSTRAP_PASSWORD,
                               roles=["SUPER_ADMIN"])
    log.info("admin_bootstrapped", extra={"username": admin.username})
    return admin


async def create_admin(db: AsyncSession, *, username: str, password: str, roles: List[str],
                       email: Optional[str] = None) -> AdminUser:
    if len(password) < 12:
        raise ValidationFailed("Admin password must be at least 12 characters")
    if (await db.execute(select(AdminUser.id).where(AdminUser.username == username))).scalar_one_or_none():
        raise Conflict("Username already exists")
    admin = AdminUser(username=username, email=email, password_hash=hash_password(password), is_active=True)
    db.add(admin)
    await db.flush()
    await set_admin_roles(db, admin, roles)
    return admin


async def set_admin_roles(db: AsyncSession, admin: AdminUser, roles: List[str]) -> AdminUser:
    bad = [r for r in roles if r not in ALL_ROLES]
    if bad:
        raise ValidationFailed("Unknown roles", details={"invalid": bad, "allowed": list(ALL_ROLES)})
    role_rows = list((await db.execute(select(Role).where(Role.name.in_(roles)))).scalars())
    for ur in list((await db.execute(select(UserRole).where(UserRole.admin_id == admin.id))).scalars()):
        await db.delete(ur)
    await db.flush()
    for r in role_rows:
        db.add(UserRole(admin_id=admin.id, role_id=r.id))
    await db.flush()
    db.expire(admin, ["roles"])
    return await _load_admin(db, admin.id)


async def _load_admin(db: AsyncSession, admin_id: uuid.UUID) -> AdminUser:
    q = select(AdminUser).where(AdminUser.id == admin_id).options(selectinload(AdminUser.roles).selectinload(Role.permissions))
    return (await db.execute(q)).scalar_one()


async def list_admins(db: AsyncSession) -> List[AdminUser]:
    q = select(AdminUser).order_by(AdminUser.created_at).options(selectinload(AdminUser.roles).selectinload(Role.permissions))
    return list((await db.execute(q)).scalars())


# --- login / sessions ---------------------------------------------------------

async def login(db: AsyncSession, username: str, password: str, ip: Optional[str],
                ua: Optional[str]) -> Tuple[AdminPrincipal, str, datetime]:
    q = (select(AdminUser).where(AdminUser.username == username)
         .options(selectinload(AdminUser.roles).selectinload(Role.permissions)))
    admin = (await db.execute(q)).scalar_one_or_none()
    # constant-ish time: always run the hash check
    ok = verify_password(password, admin.password_hash if admin else _DUMMY_HASH)
    if not admin or not ok or not admin.is_active:
        raise InvalidToken("Invalid username or password", code="INVALID_CREDENTIALS")
    ttl = get_settings().ADMIN_SESSION_TTL_MINUTES
    session = AdminSession(admin_id=admin.id, created_at=_now(), expires_at=_now() + timedelta(minutes=ttl),
                           ip_address=ip, user_agent=(ua or "")[:500])
    db.add(session)
    admin.last_login_at = _now()
    await db.flush()
    token, exp = create_admin_token(str(admin.id), str(session.id), ttl)
    return AdminPrincipal(admin=admin, session=session, permissions=permissions_for(admin),
                          roles=[r.name for r in admin.roles]), token, exp


async def authenticate(db: AsyncSession, token: str) -> AdminPrincipal:
    payload = decode_admin_token(token)
    session = await db.get(AdminSession, uuid.UUID(payload["sid"]))
    if session is None or session.revoked_at is not None:
        raise TokenRevoked("Admin session revoked")
    if session.expires_at < _now():
        raise InvalidToken("Admin session expired", code="TOKEN_EXPIRED", status_code=401)
    admin = await _load_admin(db, uuid.UUID(payload["sub"]))
    if not admin.is_active:
        raise PermissionDenied("Admin account disabled")
    return AdminPrincipal(admin=admin, session=session, permissions=permissions_for(admin),
                          roles=[r.name for r in admin.roles])


async def logout(db: AsyncSession, principal: AdminPrincipal) -> None:
    principal.session.revoked_at = _now()
    await db.flush()


# --- user management ----------------------------------------------------------

async def list_users(db: AsyncSession, *, q: Optional[str] = None, status: Optional[str] = None,
                     package_id: Optional[uuid.UUID] = None, limit: int = 50, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    base = select(Profile).where(Profile.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        base = base.where((Profile.email.ilike(like)) | (Profile.name.ilike(like)))
    if status:
        base = base.where(Profile.status == status)
    if package_id:
        sub_q = select(Subscription.tenant_id).where(Subscription.package_id == package_id,
                                                     Subscription.status.in_(["trial", "active"]))
        base = base.where(Profile.tenant_id.in_(sub_q))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    profiles = list((await db.execute(base.order_by(Profile.created_at.desc()).limit(limit).offset(offset))).scalars())
    out = []
    for p in profiles:
        out.append(await user_detail(db, p, brief=True))
    return out, total


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> Profile:
    p = await db.get(Profile, user_id)
    if p is None:
        raise UserNotFound()
    return p


async def user_detail(db: AsyncSession, p: Profile, brief: bool = False) -> Dict[str, Any]:
    from app.services import package_service
    sub = await package_service.get_current_subscription(db, p.tenant_id)
    d: Dict[str, Any] = {"id": str(p.id), "email": p.email, "name": p.name, "avatar_url": p.avatar_url, "status": p.status,
         "role": p.role, "tenant_id": str(p.tenant_id), "tenant_status": p.tenant.status,
         "created_at": p.created_at, "updated_at": p.updated_at, "last_login_at": p.last_login_at,
         "package": {"id": str(sub.package.id), "name": sub.package.name} if sub else None,
         "subscription": {"status": sub.status, "started_at": sub.started_at, "expired_at": sub.expired_at} if sub else None}
    if not brief:
        from app.services.face_service import tenant_counts
        d["counts"] = await tenant_counts(db, p.tenant_id)
    return d


async def set_user_status(db: AsyncSession, p: Profile, status: str) -> Profile:
    if status not in ("active", "suspended"):
        raise ValidationFailed("status must be 'active' or 'suspended'")
    p.status = status
    p.tenant.status = status  # suspending an owner suspends the tenant (API keys stop working)
    await db.flush()
    return p


async def set_user_role(db: AsyncSession, p: Profile, role: str) -> Profile:
    if role not in ("owner", "member"):
        raise ValidationFailed("role must be 'owner' or 'member'")
    p.role = role
    await db.flush()
    return p


async def soft_delete_user(db: AsyncSession, p: Profile) -> None:
    p.status = "deleted"
    p.deleted_at = _now()
    p.tenant.status = "suspended"
    await db.flush()


async def user_faces(db: AsyncSession, tenant_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Face], int]:
    from app.services.face_service import list_faces
    return await list_faces(db, tenant_id, limit=limit, offset=offset)


async def user_logs(db: AsyncSession, model, tenant_id: uuid.UUID, limit: int, offset: int) -> List[Any]:
    q = select(model).where(model.tenant_id == tenant_id).order_by(model.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars())


# --- dashboard ------------------------------------------------------------------

async def dashboard(db: AsyncSession) -> Dict[str, Any]:
    from app.services.usage_service import current_period_bounds, totals_today

    async def count(model, *where):
        return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()

    users_total = await count(Profile, Profile.deleted_at.is_(None))
    users_active = await count(Profile, Profile.status == "active", Profile.deleted_at.is_(None))
    users_suspended = await count(Profile, Profile.status == "suspended")
    persons = await count(Person, Person.deleted_at.is_(None))
    faces = await count(Face, Face.deleted_at.is_(None))
    images = await count(Image, Image.deleted_at.is_(None))
    today = await totals_today(db)

    dist_rows = (await db.execute(
        select(Package.name, func.count(Subscription.id)).join(Subscription, Subscription.package_id == Package.id)
        .where(Subscription.status.in_(["trial", "active"])).group_by(Package.name))).all()

    start, _ = current_period_bounds()
    period_rows = (await db.execute(
        select(func.coalesce(func.sum(UsagePeriod.api_request_count), 0), func.coalesce(func.sum(UsagePeriod.storage_used), 0),
               func.coalesce(func.sum(UsagePeriod.upload_count), 0), func.coalesce(func.sum(UsagePeriod.search_count), 0))
        .where(UsagePeriod.period_start == start))).one()
    storage_total = (await db.execute(select(func.coalesce(func.sum(Image.size_bytes), 0)).where(Image.deleted_at.is_(None)))).scalar_one()

    return {
        "users": {"total": users_total, "active": users_active, "suspended": users_suspended},
        "faces": {"persons": persons, "faces": faces, "images": images},
        "today": {"uploads": today["upload"], "searches": today["search"]},
        "current_period": {"start": start, "api_requests": int(period_rows[0]), "uploads": int(period_rows[2]),
                           "searches": int(period_rows[3])},
        "package_distribution": {name: int(n) for name, n in dist_rows},
        "storage": {"used_bytes": int(storage_total)},
        "generated_at": _now(),
    }
