"""FastAPI dependencies implementing the mandatory request flow:

    Authentication → User status → Tenant → Permission/scope → Package status → Rate limit → (Quota in service)

`get_principal` accepts either `Authorization: Bearer <supabase jwt>` or `X-API-Key: <key>`
(also `Authorization: Bearer fr_live_...`). Admin endpoints use a separate token.
"""
from typing import Optional

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.exceptions import AdminOnly, AuthRequired, InsufficientScope
from app.core.logging import api_key_id_ctx, user_id_ctx
from app.services import admin_service, auth_service, package_service, rate_limit_service, usage_service
from app.services.admin_service import AdminPrincipal
from app.services.auth_service import Principal
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.storage_service import StorageService, get_storage_service

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="SupabaseJWT",
                           description="Supabase access token (Google OAuth) — `Authorization: Bearer <jwt>`")
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKey",
                              description="API key created via /api/v1/me/api-keys")
admin_scheme = HTTPBearer(auto_error=False, scheme_name="AdminToken",
                          description="Admin session token from /api/v1/admin/auth/login")


def settings_dep() -> Settings:
    return get_settings()


def embedding_dep() -> EmbeddingService:
    return get_embedding_service()


def storage_dep() -> StorageService:
    return get_storage_service()


async def get_principal(request: Request, db: AsyncSession = Depends(get_db),
                        bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
                        api_key: Optional[str] = Security(api_key_scheme)) -> Principal:
    """Authenticates, checks account status, loads tenant + subscription, applies the package rate limit."""
    s = get_settings()
    principal: Optional[Principal] = None
    if api_key:
        principal = await auth_service.authenticate_api_key(db, api_key)
    elif bearer and bearer.credentials:
        token = bearer.credentials
        if token.startswith(s.API_KEY_PREFIX):
            principal = await auth_service.authenticate_api_key(db, token)
        else:
            principal = await auth_service.authenticate_jwt(db, token)
    if principal is None:
        raise AuthRequired()

    user_id_ctx.set(str(principal.user_id))
    if principal.api_key_id:
        api_key_id_ctx.set(str(principal.api_key_id))
    request.state.principal = principal

    package_service.assert_subscription_usable(principal.subscription)
    rl = await rate_limit_service.check_rate_limit(principal.tenant_id, principal.package.api_rate_limit)
    request.state.rate_limit = rl
    await usage_service.record_api_request(principal.tenant_id)
    return principal


async def get_jwt_principal(principal: Principal = Depends(get_principal)) -> Principal:
    """Endpoints that manage the account itself (API keys, profile) require an interactive login, not a key."""
    if principal.auth_type != "jwt":
        raise InsufficientScope("This endpoint requires a user session (Supabase JWT), not an API key")
    return principal


def require_scope(scope: str):
    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.auth_type == "api_key" and scope not in principal.scopes:
            raise InsufficientScope(details={"required_scope": scope, "granted": principal.scopes})
        return principal
    return _dep


# --- admin ---

async def get_admin(db: AsyncSession = Depends(get_db),
                    cred: Optional[HTTPAuthorizationCredentials] = Security(admin_scheme)) -> AdminPrincipal:
    if not cred or not cred.credentials:
        raise AdminOnly("Admin authentication required")
    admin = await admin_service.authenticate(db, cred.credentials)
    user_id_ctx.set(f"admin:{admin.admin.id}")
    return admin


def require_permission(perm: str):
    async def _dep(admin: AdminPrincipal = Depends(get_admin)) -> AdminPrincipal:
        admin.require(perm)
        return admin
    return _dep
