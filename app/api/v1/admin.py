"""Admin API: separate username/password auth, RBAC-enforced, every sensitive action audited."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin, require_permission
from app.core import permissions as P
from app.core.database import get_db
from app.core.exceptions import PermissionDenied
from app.core.responses import ok
from app.models import SearchLog, UploadLog
from app.schemas.common import SuccessResponse, error_responses
from app.schemas.models import (
    AdminCreate,
    AdminLogin,
    AdminRolesUpdate,
    PackageCreate,
    PackageOut,
    PackageUpdate,
    UserPackagePatch,
    UserPatch,
    UserRolePatch,
    UserStatusPatch,
)
from app.services import admin_service, audit_service, face_service, package_service, usage_service
from app.services.admin_service import AdminPrincipal
from app.services.storage_service import get_storage_service

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN_ERRORS = error_responses(401, 403)


def _admin_out(a: AdminPrincipal) -> dict:
    return {"id": str(a.admin.id), "username": a.admin.username, "email": a.admin.email, "roles": a.roles,
            "permissions": sorted(a.permissions), "last_login_at": a.admin.last_login_at}


# --- auth ---

@router.post("/auth/login", response_model=SuccessResponse[dict], responses=error_responses(401, 422), summary="Admin login",
             description="Username/password (Argon2id). Returns a session token for `Authorization: Bearer`.")
async def admin_login(body: AdminLogin, request: Request, db: AsyncSession = Depends(get_db)):
    principal, token, exp = await admin_service.login(db, body.username, body.password,
                                                      audit_service.client_ip(request), request.headers.get("user-agent"))
    await audit_service.record(db, action="admin_login", actor_type="admin", actor_id=principal.admin.id,
                               resource_type="admin_user", resource_id=principal.admin.id, request=request)
    return ok({"token": token, "token_type": "bearer", "expires_at": exp, "admin": _admin_out(principal)})


@router.post("/auth/logout", response_model=SuccessResponse[dict], responses=ADMIN_ERRORS, summary="Admin logout")
async def admin_logout(request: Request, admin: AdminPrincipal = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    await admin_service.logout(db, admin)
    await audit_service.record(db, action="admin_logout", actor_type="admin", actor_id=admin.admin.id, request=request)
    return ok({"logged_out": True})


@router.get("/auth/me", response_model=SuccessResponse[dict], responses=ADMIN_ERRORS, summary="Current admin")
async def admin_me(admin: AdminPrincipal = Depends(get_admin)):
    return ok(_admin_out(admin))


# --- admin accounts (admin.manage) ---

@router.get("/admins", response_model=SuccessResponse[list], responses=ADMIN_ERRORS, summary="List admin users")
async def list_admins(admin: AdminPrincipal = Depends(require_permission(P.ADMIN_MANAGE)), db: AsyncSession = Depends(get_db)):
    rows = await admin_service.list_admins(db)
    return ok([{"id": str(a.id), "username": a.username, "email": a.email, "is_active": a.is_active,
                "roles": [r.name for r in a.roles], "last_login_at": a.last_login_at} for a in rows])


@router.post("/admins", response_model=SuccessResponse[dict], status_code=201, responses={**ADMIN_ERRORS, **error_responses(409, 422)},
             summary="Create admin user")
async def create_admin(body: AdminCreate, request: Request, admin: AdminPrincipal = Depends(require_permission(P.ADMIN_MANAGE)),
                       db: AsyncSession = Depends(get_db)):
    a = await admin_service.create_admin(db, username=body.username, password=body.password, roles=body.roles, email=body.email)
    await audit_service.record(db, action="admin_created", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="admin_user", resource_id=a.id, metadata={"username": a.username, "roles": body.roles},
                               request=request)
    return ok({"id": str(a.id), "username": a.username, "roles": [r.name for r in a.roles]})


@router.patch("/admins/{admin_id}/roles", response_model=SuccessResponse[dict], responses={**ADMIN_ERRORS, **error_responses(404)},
              summary="Change admin roles")
async def set_admin_roles(admin_id: uuid.UUID, body: AdminRolesUpdate, request: Request,
                          admin: AdminPrincipal = Depends(require_permission(P.ADMIN_MANAGE)), db: AsyncSession = Depends(get_db)):
    target = await admin_service._load_admin(db, admin_id)
    if target.id == admin.admin.id and "SUPER_ADMIN" not in body.roles and "SUPER_ADMIN" in admin.roles:
        raise PermissionDenied("You cannot remove your own SUPER_ADMIN role")
    target = await admin_service.set_admin_roles(db, target, body.roles)
    await audit_service.record(db, action="admin_roles_changed", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="admin_user", resource_id=target.id, metadata={"roles": body.roles}, request=request)
    return ok({"id": str(target.id), "roles": [r.name for r in target.roles]})


# --- dashboard ---

@router.get("/dashboard", response_model=SuccessResponse[dict], responses=ADMIN_ERRORS, summary="Platform dashboard")
async def dashboard(admin: AdminPrincipal = Depends(require_permission(P.USAGE_READ)), db: AsyncSession = Depends(get_db)):
    return ok(await admin_service.dashboard(db))


# --- users ---

@router.get("/users", response_model=SuccessResponse[list], responses=ADMIN_ERRORS, summary="List/search users")
async def list_users(q: Optional[str] = Query(None, max_length=200), status: Optional[str] = None,
                     package_id: Optional[uuid.UUID] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     admin: AdminPrincipal = Depends(require_permission(P.USERS_READ)), db: AsyncSession = Depends(get_db)):
    rows, total = await admin_service.list_users(db, q=q, status=status, package_id=package_id, limit=limit, offset=offset)
    return ok(rows, meta={"total": total, "limit": limit, "offset": offset})


@router.get("/users/{user_id}", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="Get user")
async def get_user(user_id: uuid.UUID, request: Request, admin: AdminPrincipal = Depends(require_permission(P.USERS_READ)),
                   db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    await audit_service.record(db, action="user_viewed", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="profile", resource_id=p.id, request=request)
    return ok(await admin_service.user_detail(db, p))


@router.patch("/users/{user_id}", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="Update user")
async def patch_user(user_id: uuid.UUID, body: UserPatch, request: Request,
                     admin: AdminPrincipal = Depends(require_permission(P.USERS_WRITE)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        p.name = changes["name"]
    if "status" in changes:
        await admin_service.set_user_status(db, p, changes["status"])
    await audit_service.record(db, action="user_updated", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="profile", resource_id=p.id, metadata=changes, request=request)
    return ok(await admin_service.user_detail(db, p))


@router.patch("/users/{user_id}/role", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="Change user role")
async def patch_user_role(user_id: uuid.UUID, body: UserRolePatch, request: Request,
                          admin: AdminPrincipal = Depends(require_permission(P.USERS_WRITE)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    old = p.role
    await admin_service.set_user_role(db, p, body.role)
    await audit_service.record(db, action="user_role_changed", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="profile", resource_id=p.id, metadata={"from": old, "to": body.role}, request=request)
    return ok(await admin_service.user_detail(db, p))


@router.patch("/users/{user_id}/package", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="Change user package")
async def patch_user_package(user_id: uuid.UUID, body: UserPackagePatch, request: Request,
                             admin: AdminPrincipal = Depends(require_permission(P.USERS_WRITE)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    old = await package_service.get_current_subscription(db, p.tenant_id)
    sub = await package_service.change_package(db, p.tenant_id, body.package_id, body.status, body.expired_at)
    await audit_service.record(db, action="user_package_changed", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="profile", resource_id=p.id,
                               metadata={"from": old.package.name if old else None, "to": sub.package.name, "status": sub.status},
                               request=request)
    return ok(await admin_service.user_detail(db, p))


@router.patch("/users/{user_id}/status", response_model=SuccessResponse[dict], responses={**ADMIN_ERRORS, **error_responses(404)},
              summary="Suspend / activate user")
async def patch_user_status(user_id: uuid.UUID, body: UserStatusPatch, request: Request,
                            admin: AdminPrincipal = Depends(require_permission(P.USERS_WRITE)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    await admin_service.set_user_status(db, p, body.status)
    await audit_service.record(db, action="user_suspended" if body.status == "suspended" else "user_activated",
                               actor_type="admin", actor_id=admin.admin.id, resource_type="profile", resource_id=p.id,
                               metadata={"reason": body.reason}, request=request)
    return ok(await admin_service.user_detail(db, p))


@router.get("/users/{user_id}/usage", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="User usage")
async def user_usage(user_id: uuid.UUID, admin: AdminPrincipal = Depends(require_permission(P.USAGE_READ)),
                     db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    sub = await package_service.ensure_subscription(db, p.tenant_id)
    summary = await usage_service.usage_summary(db, p.tenant_id, sub)
    history = await usage_service.usage_history(db, p.tenant_id)
    summary["history"] = [{"period_start": r.period_start, "period_end": r.period_end, "upload_count": r.upload_count,
                           "search_count": r.search_count, "storage_used": r.storage_used, "api_request_count": r.api_request_count}
                          for r in history]
    return ok(summary)


@router.get("/users/{user_id}/faces", response_model=SuccessResponse[list],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="User faces")
async def user_faces(user_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     admin: AdminPrincipal = Depends(require_permission(P.FACES_READ)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    rows, total = await admin_service.user_faces(db, p.tenant_id, limit, offset)
    return ok([await face_service.face_to_dict(f) for f in rows], meta={"total": total, "limit": limit, "offset": offset})


@router.delete("/users/{user_id}/faces/{face_id}", response_model=SuccessResponse[dict], responses={**ADMIN_ERRORS, **error_responses(404)},
               summary="Delete a user's face")
async def admin_delete_face(user_id: uuid.UUID, face_id: uuid.UUID, request: Request,
                            admin: AdminPrincipal = Depends(require_permission(P.FACES_DELETE)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    f = await face_service.delete_face(db, p.tenant_id, face_id, get_storage_service())
    await audit_service.record(db, action="face_deleted", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="face", resource_id=f.id, metadata={"user_id": str(user_id)}, request=request)
    return ok({"deleted": True, "face_id": str(f.id)})


@router.get("/users/{user_id}/uploads", response_model=SuccessResponse[list], responses=ADMIN_ERRORS, summary="User upload history")
async def user_uploads(user_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                       admin: AdminPrincipal = Depends(require_permission(P.USAGE_READ)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    rows = await admin_service.user_logs(db, UploadLog, p.tenant_id, limit, offset)
    return ok([{"id": str(r.id), "image_id": r.image_id, "person_id": r.person_id, "file_size": r.file_size,
                "processing_time_ms": r.processing_time_ms, "status": r.status, "error_code": r.error_code,
                "api_key_id": r.api_key_id, "created_at": r.created_at} for r in rows])


@router.get("/users/{user_id}/searches", response_model=SuccessResponse[list], responses=ADMIN_ERRORS, summary="User search history")
async def user_searches(user_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        admin: AdminPrincipal = Depends(require_permission(P.USAGE_READ)), db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    rows = await admin_service.user_logs(db, SearchLog, p.tenant_id, limit, offset)
    return ok([{"id": str(r.id), "matched_person_id": r.matched_person_id, "similarity": r.similarity,
                "result_count": r.result_count, "processing_time_ms": r.processing_time_ms, "status": r.status,
                "error_code": r.error_code, "api_key_id": r.api_key_id, "created_at": r.created_at} for r in rows])


@router.delete("/users/{user_id}", response_model=SuccessResponse[dict], responses={**ADMIN_ERRORS, **error_responses(404)},
               summary="Delete user (soft)",
               description="Marks the profile deleted and suspends the tenant. Face data is retained for audit; purge separately.")
async def delete_user(user_id: uuid.UUID, request: Request, admin: AdminPrincipal = Depends(require_permission(P.USERS_DELETE)),
                      db: AsyncSession = Depends(get_db)):
    p = await admin_service.get_user(db, user_id)
    await admin_service.soft_delete_user(db, p)
    await audit_service.record(db, action="user_deleted", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="profile", resource_id=p.id, request=request)
    return ok({"deleted": True, "user_id": str(p.id)})


# --- packages ---

@router.get("/packages", response_model=SuccessResponse[list[PackageOut]], responses=ADMIN_ERRORS, summary="List packages (incl. inactive)")
async def admin_list_packages(admin: AdminPrincipal = Depends(require_permission(P.PACKAGES_READ)), db: AsyncSession = Depends(get_db)):
    return ok([PackageOut.model_validate(p).model_dump() for p in await package_service.list_packages(db, include_inactive=True)])


@router.post("/packages", response_model=SuccessResponse[PackageOut], status_code=201,
              responses={**ADMIN_ERRORS, **error_responses(409, 422)},
             summary="Create package")
async def create_package(body: PackageCreate, request: Request, admin: AdminPrincipal = Depends(require_permission(P.PACKAGES_WRITE)),
                         db: AsyncSession = Depends(get_db)):
    pkg = await package_service.create_package(db, body.model_dump())
    await audit_service.record(db, action="package_created", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="package", resource_id=pkg.id, metadata=body.model_dump(), request=request)
    return ok(PackageOut.model_validate(pkg).model_dump())


@router.get("/packages/{package_id}", response_model=SuccessResponse[PackageOut],
              responses={**ADMIN_ERRORS, **error_responses(404)}, summary="Get package")
async def get_package(package_id: uuid.UUID, admin: AdminPrincipal = Depends(require_permission(P.PACKAGES_READ)),
                      db: AsyncSession = Depends(get_db)):
    return ok(PackageOut.model_validate(await package_service.get_package(db, package_id)).model_dump())


@router.patch("/packages/{package_id}", response_model=SuccessResponse[PackageOut],
              responses={**ADMIN_ERRORS, **error_responses(404, 409)}, summary="Update package")
async def update_package(package_id: uuid.UUID, body: PackageUpdate, request: Request,
                         admin: AdminPrincipal = Depends(require_permission(P.PACKAGES_WRITE)), db: AsyncSession = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    pkg = await package_service.update_package(db, package_id, changes)
    await audit_service.record(db, action="package_updated", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="package", resource_id=pkg.id, metadata=changes, request=request)
    return ok(PackageOut.model_validate(pkg).model_dump())


@router.delete("/packages/{package_id}", response_model=SuccessResponse[dict],
              responses={**ADMIN_ERRORS, **error_responses(404, 409)}, summary="Delete package",
               description="Hard-deletes an unused package; a package with subscription history is deactivated instead (history is kept).")
async def delete_package(package_id: uuid.UUID, request: Request, admin: AdminPrincipal = Depends(require_permission(P.PACKAGES_DELETE)),
                         db: AsyncSession = Depends(get_db)):
    mode = await package_service.delete_package(db, package_id)
    await audit_service.record(db, action="package_deleted", actor_type="admin", actor_id=admin.admin.id,
                               resource_type="package", resource_id=package_id, metadata={"mode": mode}, request=request)
    return ok({"deleted": True, "mode": mode, "package_id": str(package_id)})


# --- audit logs ---

@router.get("/audit-logs", response_model=SuccessResponse[list], responses=ADMIN_ERRORS, summary="Audit logs")
async def audit_logs(action: Optional[str] = None, actor_id: Optional[uuid.UUID] = None, resource_type: Optional[str] = None,
                     resource_id: Optional[str] = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     admin: AdminPrincipal = Depends(require_permission(P.AUDIT_READ)), db: AsyncSession = Depends(get_db)):
    rows = await audit_service.query(db, action=action, actor_id=actor_id, resource_type=resource_type,
                                     resource_id=resource_id, limit=limit, offset=offset)
    return ok([{"id": str(r.id), "actor_id": r.actor_id, "actor_type": r.actor_type, "action": r.action,
                "resource_type": r.resource_type, "resource_id": r.resource_id, "metadata": r.metadata_,
                "ip_address": r.ip_address, "user_agent": r.user_agent, "request_id": r.request_id,
                "created_at": r.created_at} for r in rows], meta={"limit": limit, "offset": offset})
