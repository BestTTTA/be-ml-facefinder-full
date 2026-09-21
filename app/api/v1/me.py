"""Current user: profile, status, package, quota/usage, API keys."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_jwt_principal, get_principal
from app.core.database import get_db
from app.core.responses import ok
from app.schemas.common import AUTH_ERRORS, SuccessResponse
from app.schemas.models import PackageOut, ProfileOut, ProfileUpdate
from app.services import usage_service
from app.services.auth_service import Principal
from app.services.face_service import tenant_counts

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=SuccessResponse[ProfileOut], responses=AUTH_ERRORS, summary="Current user")
async def me(principal: Principal = Depends(get_principal)):
    return ok(ProfileOut.model_validate(principal.profile).model_dump(),
              meta={"auth_type": principal.auth_type, "scopes": principal.scopes})


@router.get("/profile", response_model=SuccessResponse[ProfileOut], responses=AUTH_ERRORS, summary="Profile")
async def profile(principal: Principal = Depends(get_principal)):
    return ok(ProfileOut.model_validate(principal.profile).model_dump())


@router.patch("/profile", response_model=SuccessResponse[ProfileOut], responses=AUTH_ERRORS, summary="Update profile")
async def update_profile(body: ProfileUpdate, principal: Principal = Depends(get_jwt_principal),
                         db: AsyncSession = Depends(get_db)):
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(principal.profile, k, v)
    await db.flush()
    return ok(ProfileOut.model_validate(principal.profile).model_dump())


@router.get("/status", response_model=SuccessResponse[dict], responses=AUTH_ERRORS, summary="Account status")
async def status(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    sub = principal.subscription
    return ok({"status": principal.profile.status, "tenant_id": str(principal.tenant_id),
               "tenant_status": principal.tenant.status,
               "subscription": {"status": sub.status, "package": sub.package.name, "expired_at": sub.expired_at},
               "counts": await tenant_counts(db, principal.tenant_id)})


@router.get("/package", response_model=SuccessResponse[PackageOut], responses=AUTH_ERRORS, summary="Current package")
async def package(principal: Principal = Depends(get_principal)):
    return ok(PackageOut.model_validate(principal.package).model_dump(),
              meta={"subscription_status": principal.subscription.status})


@router.get("/quota", response_model=SuccessResponse[dict], responses=AUTH_ERRORS, summary="Quota (used/limit/remaining)")
@router.get("/usage", response_model=SuccessResponse[dict], responses=AUTH_ERRORS, summary="Usage for the current period")
async def usage(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return ok(await usage_service.usage_summary(db, principal.tenant_id, principal.subscription))


@router.get("/usage/history", response_model=SuccessResponse[list], responses=AUTH_ERRORS, summary="Past usage periods")
async def usage_history(principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db), limit: int = 12):
    rows = await usage_service.usage_history(db, principal.tenant_id, min(limit, 60))
    return ok([{"period_start": r.period_start, "period_end": r.period_end, "upload_count": r.upload_count,
                "search_count": r.search_count, "storage_used": r.storage_used, "api_request_count": r.api_request_count}
               for r in rows])
