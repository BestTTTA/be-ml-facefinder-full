"""API key management. Requires an interactive session (Supabase JWT) — never another API key.
Mounted at both /api/v1/me/api-keys and /api/v1/api-keys."""
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_jwt_principal
from app.core.database import get_db
from app.core.responses import ok
from app.schemas.common import AUTH_ERRORS, SuccessResponse, error_responses
from app.schemas.models import ApiKeyCreate, ApiKeyCreatedOut, ApiKeyOut
from app.services import api_key_service, audit_service
from app.services.auth_service import Principal

router = APIRouter(tags=["api-keys"])


@router.post("", response_model=SuccessResponse[ApiKeyCreatedOut], status_code=201,
             responses={**AUTH_ERRORS, **error_responses(422)}, summary="Create API key",
             description="The full key is returned once and never stored. Only its SHA-256 hash is kept.")
async def create_api_key(body: ApiKeyCreate, request: Request, principal: Principal = Depends(get_jwt_principal),
                         db: AsyncSession = Depends(get_db)):
    expires_at = api_key_service.resolve_expiry(body.expires_in, body.expires_at)
    key, raw = await api_key_service.create_key(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                                                name=body.name, scopes=body.scopes, expires_at=expires_at)
    await audit_service.record(db, action="api_key_created", actor_type="user", actor_id=principal.user_id,
                               resource_type="api_key", resource_id=key.id,
                               metadata={"name": key.name, "scopes": key.scopes}, request=request)
    return ok({**ApiKeyOut.model_validate(key).model_dump(), "api_key": raw})


@router.get("", response_model=SuccessResponse[list[ApiKeyOut]], responses=AUTH_ERRORS, summary="List API keys")
async def list_api_keys(principal: Principal = Depends(get_jwt_principal), db: AsyncSession = Depends(get_db)):
    keys = await api_key_service.list_keys(db, principal.user_id)
    return ok([ApiKeyOut.model_validate(k).model_dump() for k in keys])


@router.delete("/{key_id}", response_model=SuccessResponse[ApiKeyOut], responses={**AUTH_ERRORS, **error_responses(404)},
               summary="Revoke API key", description="Revocation is immediate.")
async def revoke_api_key(key_id: uuid.UUID, request: Request, principal: Principal = Depends(get_jwt_principal),
                         db: AsyncSession = Depends(get_db)):
    key = await api_key_service.get_owned_key(db, principal.user_id, key_id)
    await api_key_service.revoke_key(db, key)
    await audit_service.record(db, action="api_key_revoked", actor_type="user", actor_id=principal.user_id,
                               resource_type="api_key", resource_id=key.id, request=request)
    return ok(ApiKeyOut.model_validate(key).model_dump())


@router.post("/{key_id}/rotate", response_model=SuccessResponse[ApiKeyCreatedOut],
             responses={**AUTH_ERRORS, **error_responses(404)}, summary="Rotate API key",
             description="Revokes the key and returns a new one with the same name, scopes and expiry.")
async def rotate_api_key(key_id: uuid.UUID, request: Request, principal: Principal = Depends(get_jwt_principal),
                         db: AsyncSession = Depends(get_db)):
    old = await api_key_service.get_owned_key(db, principal.user_id, key_id)
    new, raw = await api_key_service.rotate_key(db, old)
    await audit_service.record(db, action="api_key_rotated", actor_type="user", actor_id=principal.user_id,
                               resource_type="api_key", resource_id=new.id, metadata={"rotated_from": str(old.id)},
                               request=request)
    return ok({**ApiKeyOut.model_validate(new).model_dump(), "api_key": raw})
