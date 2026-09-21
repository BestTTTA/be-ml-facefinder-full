"""Supabase Auth (GoTrue) integration. Supabase is the source of truth for user identity.

JWT verification supports both project styles:
  * legacy HS256 projects  -> SUPABASE_JWT_SECRET
  * new asymmetric keys    -> JWKS at {SUPABASE_URL}/auth/v1/.well-known/jwks.json (ES256/RS256)
"""
from typing import Any, Dict, Optional

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings, get_settings
from app.core.exceptions import InvalidToken, ServiceUnavailable, TokenExpired
from app.core.logging import get_logger

log = get_logger(__name__)


class SupabaseService:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or get_settings()
        self._jwks: Optional[PyJWKClient] = None
        self._http = httpx.AsyncClient(timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.s.SUPABASE_JWT_SECRET or self.s.SUPABASE_URL)

    def _jwks_client(self) -> PyJWKClient:
        if self._jwks is None:
            if not self.s.SUPABASE_URL:
                raise ServiceUnavailable("Supabase is not configured (SUPABASE_URL / SUPABASE_JWT_SECRET)")
            self._jwks = PyJWKClient(f"{self.s.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json",
                                     cache_keys=True, lifespan=3600)
        return self._jwks

    def verify_access_token(self, token: str) -> Dict[str, Any]:
        """Returns validated claims. Raises TokenExpired / InvalidToken."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise InvalidToken() from e
        alg = header.get("alg", "HS256")
        try:
            if alg == "HS256":
                if not self.s.SUPABASE_JWT_SECRET:
                    raise InvalidToken("HS256 token but SUPABASE_JWT_SECRET is not configured")
                claims = jwt.decode(token, self.s.SUPABASE_JWT_SECRET, algorithms=["HS256"],
                                    audience=self.s.SUPABASE_JWT_AUDIENCE)
            else:
                key = self._jwks_client().get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["ES256", "RS256"], audience=self.s.SUPABASE_JWT_AUDIENCE)
        except jwt.ExpiredSignatureError as e:
            raise TokenExpired() from e
        except jwt.PyJWTError as e:
            raise InvalidToken(details={"reason": type(e).__name__}) from e
        if not claims.get("sub"):
            raise InvalidToken("Token has no subject")
        return claims

    # --- GoTrue REST -----------------------------------------------------
    def _auth_headers(self) -> Dict[str, str]:
        if not (self.s.SUPABASE_URL and self.s.SUPABASE_ANON_KEY):
            raise ServiceUnavailable("Supabase is not configured (SUPABASE_URL / SUPABASE_ANON_KEY)")
        return {"apikey": self.s.SUPABASE_ANON_KEY, "Content-Type": "application/json"}

    @property
    def _base(self) -> str:
        return (self.s.SUPABASE_URL or "").rstrip("/")

    async def refresh_session(self, refresh_token: str) -> Dict[str, Any]:
        r = await self._http.post(f"{self._base}/auth/v1/token?grant_type=refresh_token",
                                  json={"refresh_token": refresh_token}, headers=self._auth_headers())
        if r.status_code != 200:
            raise InvalidToken("Refresh token rejected", details={"status": r.status_code})
        return r.json()

    async def logout(self, access_token: str, scope: str = "global") -> None:
        headers = {**self._auth_headers(), "Authorization": f"Bearer {access_token}"}
        r = await self._http.post(f"{self._base}/auth/v1/logout?scope={scope}", headers=headers)
        if r.status_code not in (200, 204):
            log.warning("supabase_logout_failed", extra={"status": r.status_code})

    async def ping(self) -> bool:
        if not self.s.SUPABASE_URL:
            return False
        try:
            r = await self._http.get(f"{self.s.SUPABASE_URL.rstrip('/')}/auth/v1/health",
                                     headers={"apikey": self.s.SUPABASE_ANON_KEY or ""})
            return r.status_code == 200
        except Exception:
            return False


_service: Optional[SupabaseService] = None


def get_supabase_service() -> SupabaseService:
    global _service
    if _service is None:
        _service = SupabaseService()
    return _service
