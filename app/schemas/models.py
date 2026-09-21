"""Request / response schemas for the v1 API."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.permissions import ALL_ROLES, ALL_SCOPES


# --- auth / me ---
class RefreshRequest(BaseModel):
    refresh_token: str = Field(examples=["v1.MRjq..."])


class LogoutRequest(BaseModel):
    scope: str = Field("global", pattern="^(global|local|others)$")


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    status: str
    role: str
    tenant_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    avatar_url: Optional[str] = Field(None, max_length=2000)


# --- persons ---
class PersonIn(BaseModel):
    external_user_id: Optional[str] = Field(None, max_length=200, examples=["EMP-001"])
    name: Optional[str] = Field(None, max_length=200, examples=["John Doe"])
    email: Optional[str] = Field(None, max_length=320, examples=["john@example.com"])
    phone: Optional[str] = Field(None, max_length=50, examples=["0812345678"])
    company: Optional[str] = Field(None, max_length=200, examples=["Example Company"])
    department: Optional[str] = Field(None, max_length=200, examples=["Engineering"])
    metadata: Optional[Dict[str, Any]] = Field(None, examples=[{"employee_code": "EMP-001", "position": "Engineer"}])


class PersonOut(BaseModel):
    id: uuid.UUID
    external_user_id: Optional[str]
    name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    company: Optional[str]
    department: Optional[str]
    metadata: Dict[str, Any]
    created_at: datetime
    updated_at: datetime
    face_count: Optional[int] = None
    faces: Optional[List[Dict[str, Any]]] = None


# --- faces ---
class FaceOut(BaseModel):
    id: uuid.UUID
    person_id: uuid.UUID
    image_id: uuid.UUID
    bbox: Dict[str, int]
    det_score: Optional[float]
    model: str
    created_at: datetime
    image_url: Optional[str] = Field(None, description="Signed URL, expires after SIGNED_URL_TTL_SECONDS")
    thumbnail_url: Optional[str] = None


class UploadOut(BaseModel):
    face: FaceOut
    person: PersonOut
    image: Dict[str, Any]
    processing_time_ms: int


class SearchOut(BaseModel):
    match: bool = Field(examples=[True])
    confidence: float = Field(examples=[0.94], description="Logistic calibration of similarity around the threshold")
    similarity: Optional[float] = Field(None, examples=[0.91], description="Cosine similarity of the best match")
    person: Optional[PersonOut] = None
    face_id: Optional[uuid.UUID] = None
    candidates: List[Dict[str, Any]] = Field(default_factory=list, description="Top-K persons above threshold")
    threshold: float
    query: Dict[str, Any]
    processing_time_ms: int


# --- api keys ---
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["production-server"])
    scopes: List[str] = Field(default_factory=lambda: sorted(ALL_SCOPES), examples=[["faces:upload", "faces:search"]])
    expires_in: Optional[str] = Field("90d", description="1d | 7d | 30d | 90d | 1y | never (or use expires_at)")
    expires_at: Optional[datetime] = Field(None, description="Custom absolute expiry (UTC)")

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: List[str]) -> List[str]:
        bad = sorted(set(v) - ALL_SCOPES)
        if bad:
            raise ValueError(f"Unknown scopes: {bad}; allowed: {sorted(ALL_SCOPES)}")
        return v


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: List[str]
    status: str
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    last_used_at: Optional[datetime]
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    api_key: str = Field(description="Full key. Shown ONCE — it is not stored.")


# --- packages ---
class PackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: Optional[str]
    price: float
    currency: str
    billing_cycle: str
    upload_limit: int = Field(description="-1 = unlimited")
    search_limit: int
    storage_limit: int = Field(description="bytes, -1 = unlimited")
    max_users: int
    api_rate_limit: int = Field(description="requests per minute")
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class PackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    price: float = Field(0, ge=0)
    currency: str = Field("USD", min_length=3, max_length=3)
    billing_cycle: str = Field("monthly", pattern="^(monthly|yearly|custom)$")
    upload_limit: int = Field(ge=-1)
    search_limit: int = Field(ge=-1)
    storage_limit: int = Field(ge=-1)
    max_users: int = Field(1, ge=-1)
    api_rate_limit: int = Field(ge=1)
    is_active: bool = True
    is_default: bool = False


class PackageUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    price: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    billing_cycle: Optional[str] = Field(None, pattern="^(monthly|yearly|custom)$")
    upload_limit: Optional[int] = Field(None, ge=-1)
    search_limit: Optional[int] = Field(None, ge=-1)
    storage_limit: Optional[int] = Field(None, ge=-1)
    max_users: Optional[int] = Field(None, ge=-1)
    api_rate_limit: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


# --- admin ---
class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class AdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=12, max_length=1024)
    email: Optional[str] = None
    roles: List[str] = Field(examples=[["SUPPORT"]])

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: List[str]) -> List[str]:
        bad = [r for r in v if r not in ALL_ROLES]
        if bad:
            raise ValueError(f"Unknown roles: {bad}; allowed: {list(ALL_ROLES)}")
        return v


class AdminRolesUpdate(BaseModel):
    roles: List[str]


class UserPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    status: Optional[str] = Field(None, pattern="^(active|suspended)$")


class UserRolePatch(BaseModel):
    role: str = Field(pattern="^(owner|member)$")


class UserPackagePatch(BaseModel):
    package_id: uuid.UUID
    status: str = Field("active", pattern="^(trial|active|past_due|cancelled|expired|suspended)$")
    expired_at: Optional[datetime] = None


class UserStatusPatch(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")
    reason: Optional[str] = Field(None, max_length=500)
