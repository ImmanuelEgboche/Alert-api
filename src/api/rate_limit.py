"""Fixed-window rate limiting backed by Redis.

Each (bucket, window) pair is one Redis counter that expires with its window,
so there is nothing to clean up and every API instance shares the same counts.

A fixed window can let up to 2x the limit through across a window boundary
(end of one minute + start of the next). That's an acceptable trade for one
round trip per request; the limits here are abuse guards, not billing.

Redis being down must not take the API down with it, so errors fail open
(the request is allowed and a warning is logged).
"""
import time

import structlog
from fastapi import HTTPException, Request, status

from src.config import get_settings
from src.database import get_redis

logger = structlog.get_logger()

KEY_PREFIX = "ratelimit:"


async def hit(bucket: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """Count one request against ``bucket``.

    Returns:
        (allowed, remaining, seconds_until_reset)
    """
    now = int(time.time())
    window_start = now - now % window_seconds
    key = f"{KEY_PREFIX}{bucket}:{window_start}"
    reset_in = window_start + window_seconds - now

    redis = await get_redis()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_seconds + 1)
        count, _ = await pipe.execute()

    return count <= limit, max(limit - count, 0), reset_in


async def enforce(bucket: str, limit: int, window_seconds: int) -> None:
    """Raise 429 once ``bucket`` exceeds ``limit`` requests per window.

    Raises:
        HTTPException: 429 with Retry-After when over the limit
    """
    try:
        allowed, _, reset_in = await hit(bucket, limit, window_seconds)
    except Exception as e:
        logger.warning("rate_limit_unavailable", bucket=bucket, error=str(e))
        return

    if not allowed:
        logger.info("rate_limited", bucket=bucket, limit=limit, window_seconds=window_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {limit} requests per {window_seconds}s",
            headers={
                "Retry-After": str(reset_in),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )


def client_ip(request: Request) -> str:
    """The caller's IP. Behind a reverse proxy, uvicorn rewrites this from
    X-Forwarded-For only for proxies listed in FORWARDED_ALLOW_IPS."""
    return request.client.host if request.client else "unknown"


async def limit_registrations(request: Request) -> None:
    """Dependency: cap account creation per IP (stops scripted sign-up spam)."""
    await enforce(
        f"register:{client_ip(request)}", get_settings().register_rate_limit_per_hour, 3600
    )


async def limit_user(user_id: int) -> None:
    """Cap requests per user across all authenticated endpoints."""
    await enforce(f"user:{user_id}", get_settings().rate_limit_per_minute, 60)
