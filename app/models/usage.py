"""Usage periods (monthly counters, kept forever), immutable usage/search/upload logs, audit logs."""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPk


class UsagePeriod(UUIDPk, Base):
    __tablename__ = "usage_periods"
    __table_args__ = (UniqueConstraint("tenant_id", "period_start", name="ux_usage_periods_tenant_start"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    upload_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    search_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # bytes at end of period / current
    api_request_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)


class UsageLog(UUIDPk, Base):
    """Immutable per-operation record. idempotency_key prevents double counting on retries."""
    __tablename__ = "usage_logs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="ux_usage_logs_tenant_idem"),
        Index("ix_usage_logs_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # upload|search|storage
    quantity: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    resource_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SearchLog(UUIDPk, Base):
    __tablename__ = "search_logs"
    __table_args__ = (Index("ix_search_logs_tenant_created", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    image_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))  # query images are not persisted by default
    matched_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    similarity: Mapped[Optional[float]] = mapped_column(Float)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # match|no_match|failed|rejected
    error_code: Mapped[Optional[str]] = mapped_column(String(50))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UploadLog(UUIDPk, Base):
    __tablename__ = "upload_logs"
    __table_args__ = (Index("ix_upload_logs_tenant_created", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    image_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    processing_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success|failed|rejected
    error_code: Mapped[Optional[str]] = mapped_column(String(50))
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLog(UUIDPk, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_actor", "actor_type", "actor_id"),
    )

    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)  # admin|user|system
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(50))
    resource_id: Mapped[Optional[str]] = mapped_column(String(64))
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    request_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
