"""Session endpoints backed by Supabase Auth (Google OAuth happens on the client via Supabase)."""
from typing import Optional

from fastapi import APIRouter, Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bearer_scheme, get_principal
from app.core.database import get_db
from app.core.exceptions import AuthRequired
from app.core.responses import ok
from app.schemas.common import SuccessResponse, error_responses
from app.schemas.models import LogoutRequest, RefreshRequest
from app.services import audit_service
from app.services.auth_service import Principal
from app.services.supabase_service import get_supabase_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/refresh", response_model=SuccessResponse[dict], responses=error_responses(401, 503),
             summary="Refresh a Supabase session",
             description="Exchanges a Supabase refresh token for a new access/refresh token pair. "
                         "No authentication header is required. Requires SUPABASE_URL and SUPABASE_ANON_KEY.")
async def refresh(body: RefreshRequest):
    session = await get_supabase_service().refresh_session(body.refresh_token)
    return ok({"access_token": session.get("access_token"), "refresh_token": session.get("refresh_token"),
               "token_type": session.get("token_type", "bearer"), "expires_in": session.get("expires_in"),
               "expires_at": session.get("expires_at")})


@router.post("/logout", response_model=SuccessResponse[dict], responses=error_responses(401, 503),
             summary="Logout (revoke Supabase refresh tokens)",
             description="Requires `Authorization: Bearer <supabase jwt>`. scope=global revokes every session of the user.")
async def logout(request: Request, body: Optional[LogoutRequest] = None,
                 principal: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db),
                 cred: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)):
    if principal.auth_type != "jwt" or not cred:
        raise AuthRequired("Logout requires a Supabase session token")
    await get_supabase_service().logout(cred.credentials, (body.scope if body else "global"))
    await audit_service.record(db, action="user_logout", actor_type="user", actor_id=principal.user_id,
                               resource_type="profile", resource_id=principal.user_id, request=request)
    return ok({"logged_out": True})
