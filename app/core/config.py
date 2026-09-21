"""Application settings. Every value comes from the environment / .env file."""
from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    APP_NAME: str = "Face Recognition API"
    APP_ENV: str = "development"  # development | production | test
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "*"
    LEGACY_API_ENABLED: bool = False  # mount the pre-SaaS Facemenow app under /legacy

    # --- database ---
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/facefinder"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # --- redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- supabase ---
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = None
    SUPABASE_SERVICE_ROLE_KEY: Optional[str] = None
    SUPABASE_JWT_SECRET: Optional[str] = None  # HS256 projects; if empty JWKS (ES256/RS256) is used
    SUPABASE_JWT_AUDIENCE: str = "authenticated"

    # --- storage ---
    STORAGE_BACKEND: str = "minio"  # minio | supabase
    STORAGE_BUCKET: str = "faces"
    SIGNED_URL_TTL_SECONDS: int = 900
    MINIO_ENDPOINT: Optional[str] = None
    MINIO_ACCESS_KEY: Optional[str] = None
    MINIO_SECRET_KEY: Optional[str] = None
    MINIO_SSL: bool = False
    MINIO_PUBLIC_URL: Optional[str] = None  # public address used for presigned URLs
    MINIO_REGION: str = "us-east-1"

    # --- face engine ---
    FACE_ENGINE: str = "insightface"  # insightface | fake (tests only)
    FACE_MODEL_NAME: str = "buffalo_l"
    FACE_MODEL_PATH: Optional[str] = None  # insightface root dir; defaults to ~/.insightface
    FACE_CTX_ID: int = -1  # -1 = CPU, 0 = GPU
    FACE_DET_SIZE: int = 640
    EMBEDDING_DIMENSION: int = 512
    FACE_SIMILARITY_THRESHOLD: float = 0.45
    FACE_MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024
    FACE_ALLOWED_MIME_TYPES: str = "image/jpeg,image/png,image/webp"
    FACE_ALLOW_MULTIPLE_FACES: bool = False  # upload: reject images with >1 face
    FACE_SEARCH_TOP_K: int = 5

    # --- admin ---
    ADMIN_JWT_SECRET: str = "change-me-in-production"
    ADMIN_SESSION_TTL_MINUTES: int = 480
    ADMIN_BOOTSTRAP_USERNAME: Optional[str] = None
    ADMIN_BOOTSTRAP_PASSWORD: Optional[str] = None

    # --- api keys ---
    API_KEY_PREFIX: str = "fr_live_"

    # --- rate limiting / quota ---
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    DEFAULT_PACKAGE_NAME: str = "Free"
    IDEMPOTENCY_TTL_SECONDS: int = 86400

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_mime_types(self) -> List[str]:
        return [m.strip() for m in self.FACE_ALLOWED_MIME_TYPES.split(",") if m.strip()]

    @property
    def async_database_url(self) -> str:
        url = self.DATABASE_URL
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @field_validator("FACE_SIMILARITY_THRESHOLD")
    @classmethod
    def _threshold_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("FACE_SIMILARITY_THRESHOLD must be between 0 and 1")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
