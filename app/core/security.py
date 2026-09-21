"""Password hashing (Argon2id), API-key generation/hashing, admin session JWTs."""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.exceptions import InvalidToken, TokenExpired

_ph = PasswordHasher()  # argon2id by default


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


# --- API keys -------------------------------------------------------------
# Format: <prefix><32 bytes urlsafe>. Only the SHA-256 hash is persisted;
# key_prefix (first 12 chars after the configured prefix) is stored for display.

def generate_api_key() -> Tuple[str, str, str]:
    """Returns (raw_key, key_prefix, key_hash)."""
    prefix = get_settings().API_KEY_PREFIX
    raw = prefix + secrets.token_urlsafe(32)
    return raw, raw[: len(prefix) + 8], hash_api_key(raw)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- Admin sessions -------------------------------------------------------

def create_admin_token(admin_id: str, session_id: str, ttl_minutes: int | None = None) -> Tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ttl_minutes or s.ADMIN_SESSION_TTL_MINUTES)
    payload: Dict[str, Any] = {"sub": admin_id, "sid": session_id, "typ": "admin",
                               "iat": int(now.timestamp()), "exp": int(exp.timestamp()), "jti": str(uuid.uuid4())}
    return jwt.encode(payload, s.ADMIN_JWT_SECRET, algorithm="HS256"), exp


def decode_admin_token(token: str) -> Dict[str, Any]:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.ADMIN_JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise TokenExpired("Admin session expired") from e
    except jwt.PyJWTError as e:
        raise InvalidToken("Invalid admin token") from e
    if payload.get("typ") != "admin":
        raise InvalidToken("Not an admin token")
    return payload
