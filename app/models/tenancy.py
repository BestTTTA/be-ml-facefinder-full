"""Tenants, user profiles (Supabase is the auth source of truth), packages, subscriptions."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPk


class Tenant(UUIDPk, Timestamped, Base):
    """Isolation boundary. Every person/face/image/usage row carries a tenant_id.
    Currently one tenant is created per account (owner); teams can be added later."""
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|suspended


class Profile(Timestamped, Base):
    __tablename__ = "profiles"

    # id == Supabase auth.users.id
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(200))
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|suspended|deleted
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=False)  # owner|member
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    tenant: Mapped["Tenant"] = relationship(lazy="joined")


class Package(UUIDPk, Timestamped, Base):
    __tablename__ = "packages"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    billing_cycle: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)  # monthly|yearly|custom

    upload_limit: Mapped[int] = mapped_column(Integer, nullable=False)   # per period, -1 = unlimited
    search_limit: Mapped[int] = mapped_column(Integer, nullable=False)   # per period, -1 = unlimited
    storage_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)  # bytes, -1 = unlimited
    max_users: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    api_rate_limit: Mapped[int] = mapped_column(Integer, nullable=False)  # requests per RATE_LIMIT_WINDOW

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Subscription(UUIDPk, Timestamped, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_tenant_status", "tenant_id", "status"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    package_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("packages.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # trial|active|past_due|cancelled|expired|suspended
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    renewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    external_ref: Mapped[Optional[str]] = mapped_column(String(200))  # e.g. Stripe subscription id (future)

    package: Mapped["Package"] = relationship(lazy="joined")
