"""Persons, images and face embeddings (pgvector)."""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamped, UUIDPk

EMBEDDING_DIM = 512  # buffalo_l / ArcFace. Changing this requires a migration.


class Person(UUIDPk, Timestamped, Base):
    __tablename__ = "persons"
    __table_args__ = (
        Index("ux_persons_tenant_external", "tenant_id", "external_user_id", unique=True,
              postgresql_where=text("external_user_id IS NOT NULL AND deleted_at IS NULL")),
        Index("ix_persons_tenant_name", "tenant_id", "name"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    external_user_id: Mapped[Optional[str]] = mapped_column(String(200))
    name: Mapped[Optional[str]] = mapped_column(String(200))
    email: Mapped[Optional[str]] = mapped_column(String(320))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    company: Mapped[Optional[str]] = mapped_column(String(200))
    department: Mapped[Optional[str]] = mapped_column(String(200))
    metadata_: Mapped[Dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    faces: Mapped[list["Face"]] = relationship(back_populates="person", lazy="selectin",
                                               primaryjoin="and_(Person.id==Face.person_id, Face.deleted_at==None)")


class Image(UUIDPk, Timestamped, Base):
    __tablename__ = "images"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="ux_images_tenant_sha256"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), index=True)
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_key: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Face(UUIDPk, Timestamped, Base):
    __tablename__ = "faces"
    __table_args__ = (
        Index("ix_faces_tenant_person", "tenant_id", "person_id"),
        # HNSW cosine index for tenant-scoped nearest-neighbour search.
        Index("ix_faces_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    bbox: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)  # {x1,y1,x2,y2}
    det_score: Mapped[Optional[float]] = mapped_column(Float)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    person: Mapped["Person"] = relationship(back_populates="faces")
    image: Mapped["Image"] = relationship(lazy="joined")
