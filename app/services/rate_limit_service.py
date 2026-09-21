"""Redis fixed-window rate limiter keyed by tenant (limit comes from the tenant's package)."""
import time
import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.exceptions import RateLimitExceeded
from app.core.logging import get_logger
from app.core.redis import get_redis

log = get_logger(__name__)

@dataclass
class RateLimitResult:
    limit: int
    remaining: int
    reset_epoch: int
    allowed: bool

    def headers(self) -> dict[str, str]:
        return {"X-RateLimit-Limit": str(self.limit), "X-RateLimit-Remaining": str(max(self.remaining, 0)),
                "X-RateLimit-Reset": str(self.reset_epoch)}


async def check_rate_limit(tenant_id: uuid.UUID, limit: int, bucket: str = "api") -> RateLimitResult:
    s = get_settings()
    window = s.RATE_LIMIT_WINDOW_SECONDS
    now = int(time.time())
    if not s.RATE_LIMIT_ENABLED or limit < 0:
        return RateLimitResult(limit=limit, remaining=limit, reset_epoch=now + window, allowed=True)
    key = f"ratelimit:{bucket}:{tenant_id}:{now // window}"
    try:
        # MULTI/EXEC: INCR + EXPIRE NX (set TTL only on first hit) + TTL — atomic, no Lua needed.
        pipe = get_redis().pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        pipe.ttl(key)
        n, _, ttl = await pipe.execute()
    except Exception:
        log.warning("rate_limit_redis_unavailable")  # fail open, but only for rate limiting
        return RateLimitResult(limit=limit, remaining=limit, reset_epoch=now + window, allowed=True)
    n, ttl = int(n), int(ttl)
    reset = now + (ttl if ttl > 0 else window)
    res = RateLimitResult(limit=limit, remaining=limit - n, reset_epoch=reset, allowed=n <= limit)
    if not res.allowed:
        raise RateLimitExceeded(details={"limit": limit, "window_seconds": window, "retry_after": reset - now},
                                headers={**res.headers(), "Retry-After": str(reset - now)})
    return res
