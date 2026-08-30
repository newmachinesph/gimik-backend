"""Fixed-window rate limiting over Upstash Redis, via REST.

REST rather than a Redis connection: there is no pool to size, nothing to keep
alive per worker, and it works unchanged from a container, a laptop or a
serverless runtime.

**It fails open.** If Upstash is slow or unreachable the request is allowed.
A limiter exists to keep a burst from hurting the database; letting it decide
who gets served when it is itself broken turns a dependency wobble into a total
outage. What it must never do is fail *silently* on the way in, so a limiter
error is logged every time.

The window is fixed rather than sliding, which permits a burst of up to 2x the
limit across a boundary. That is the accepted cost of one round trip per
request: a sliding log would need a sorted set and several more.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.config import get_settings
from app.deps import bearer

settings = get_settings()
log = logging.getLogger("gora.ratelimit")

TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class Limit:
    """`times` requests per `seconds`, counted per identity per bucket."""

    times: int
    seconds: int
    bucket: str


async def _incr(key: str, ttl: int) -> int | None:
    """INCR the window and set its expiry on first write. None means unknown."""
    url = settings.upstash_redis_rest_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.upstash_redis_rest_token}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            # One pipeline, one round trip: INCR then a best-effort EXPIRE. NX
            # keeps a refreshed expiry from sliding the window forward forever.
            response = await client.post(
                f"{url}/pipeline",
                headers=headers,
                json=[["INCR", key], ["EXPIRE", key, str(ttl), "NX"]],
            )
        if response.status_code >= 400:
            log.warning("upstash %s: %s", response.status_code, response.text[:200])
            return None
        payload = response.json()
        return int(payload[0]["result"])
    except Exception:
        log.exception("rate limit check failed for %s", key)
        return None


def _identity(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    """The token when there is one, the peer address when there is not.

    Deliberately the raw bearer rather than the decoded subject: limiting runs
    before authentication, and a token that fails to decode still deserves a
    ceiling - otherwise a bad token is an unlimited one.
    """
    if credentials is not None:
        return f"tok:{hash(credentials.credentials) & 0xFFFFFFFF:x}"
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
