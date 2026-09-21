"""Shared async Redis client."""
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _client


def set_redis(client: Optional[aioredis.Redis]) -> None:
    """Used by tests to inject fakeredis."""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
