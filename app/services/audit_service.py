"""Audit log writer. Metadata is redacted so passwords / raw keys can never be persisted."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import _redact, request_id_ctx
from app.models import AuditLog


def client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


async def record(db: AsyncSession, *, action: str, actor_type: str, actor_id: Optional[uuid.UUID] = None,
                 resource_type: Optional[str] = None, resource_id: Optional[Any] = None,
                 metadata: Optional[Dict[str, Any]] = None, request: Optional[Request] = None) -> AuditLog:
    entry = AuditLog(actor_id=actor_id, actor_type=actor_type, action=action, resource_type=resource_type,
                     resource_id=str(resource_id) if resource_id is not None else None,
                     metadata_=_redact(metadata or {}), ip_address=client_ip(request),
                     user_agent=(request.headers.get("user-agent", "")[:500] if request else None),
                     request_id=request_id_ctx.get())
    db.add(entry)
    await db.flush()
    return entry


async def query(db: AsyncSession, *, action: Optional[str] = None, actor_id: Optional[uuid.UUID] = None,
                resource_type: Optional[str] = None, resource_id: Optional[str] = None,
                limit: int = 50, offset: int = 0) -> List[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    if action:
        q = q.where(AuditLog.action == action)
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    return list((await db.execute(q)).scalars())
