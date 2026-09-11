"""Fixed-window rate limiting over Redis.

The compose service locally, a Railway Redis plugin in deployment. This used to
speak Upstash's REST API; the move to a plain Redis connection made the code
smaller rather than larger, because INCR and EXPIRE in a pipeline is exactly
what the REST endpoint was wrapping.

**It fails open.** If Redis is slow or unreachable the request is allowed.
A limiter exists to keep a burst from hurting the database; letting it decide
who gets served when it is itself broken turns a dependency wobble into a total
outage. What it must never do is fail *silently* on the way in, so a limiter
error is logged every time.

The window is fixed rather than sliding, which permits a burst of up to 2x the
limit across a boundary. That is the accepted cost of one round trip per
request: a sliding log would need a sorted set and several more.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.config import get_settings
from app.deps import bearer

settings = get_settings()
log = logging.getLogger("gimik.ratelimit")

TIMEOUT_SECONDS = 2.0

_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    """One pool for the process, built on first use.

    Not at import time: the module is imported by every router, and a settings
    file with no Redis configured must not cost a connection attempt.
    """
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            socket_timeout=TIMEOUT_SECONDS,
            socket_connect_timeout=TIMEOUT_SECONDS,
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    """Released on shutdown, so a reload does not leak the pool."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@dataclass(frozen=True)
class Limit:
    """`times` requests per `seconds`, counted per identity per bucket."""

    times: int
    seconds: int
    bucket: str


async def _incr(key: str, ttl: int) -> int | None:
    """INCR the window and set its expiry on first write. None means unknown."""
    try:
        pipe = _redis().pipeline()
        pipe.incr(key)
        # NX keeps a refreshed expiry from sliding the window forward forever.
        pipe.expire(key, ttl, nx=True)
        count, _ = await pipe.execute()
        return int(count)
    except Exception:
        log.exception("rate limit check failed for %s", key)
        return None


def _identity(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    """The token when there is one, the peer address when there is not.

    Deliberately the raw bearer rather than the decoded subject: limiting runs
    before authentication, and a token that fails to decode still deserves a
    ceiling - otherwise a bad token is an unlimited one.

    SHA-256 rather than the builtin `hash()`. Python salts string hashing per
    process, so the builtin gave every worker, every replica and every restart
    its own set of buckets - which quietly multiplied each limit by the number
    of processes serving it. The digest is stable everywhere and forever.
    """
    if credentials is not None:
        digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()[:16]
        return f"tok:{digest}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def limiter(limit: Limit):
    """A dependency that counts this caller's requests in a fixed window."""

    async def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if not settings.rate_limiting_enabled:
            return
        window = int(time.time()) // limit.seconds
        key = f"rl:{limit.bucket}:{_identity(request, credentials)}:{window}"
        count = await _incr(key, limit.seconds)
        if count is None or count <= limit.times:
            return
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Try again shortly.",
            headers={"Retry-After": str(limit.seconds)},
        )

    return dependency


# The ceilings. Sign-in and sign-up are the ones worth guarding hardest: they
# are unauthenticated, they hash a password, and they are what a credential
# stuffer reaches for.
SIGN_IN = Limit(times=10, seconds=300, bucket="signin")
SIGN_UP = Limit(times=5, seconds=3600, bucket="signup")
# Writes that fan out to other people - an invite, a message, a report.
SOCIAL_WRITE = Limit(times=60, seconds=60, bucket="social")
# Everything else authenticated, generous enough that no real session notices.
GENERAL = Limit(times=300, seconds=60, bucket="general")
