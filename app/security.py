"""Password hashing and token issuing, unchanged in spirit from v1.

Argon2id rather than bcrypt: bcrypt silently truncates at 72 bytes, so two long
passphrases sharing a prefix authenticate each other.

Every token carries a `tv` claim - the account's `token_version` at the moment
it was minted. JWTs are stateless, so without it nothing can end a session
early: a password change, a stolen-device sign-out and a spent reset link would
all leave a 30-day refresh token working. Bumping the column invalidates every
token issued before the bump, which is what those three actions actually mean.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import get_settings

settings = get_settings()
_hasher = PasswordHasher()

TokenKind = Literal["access", "refresh", "reset", "verify"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """False rather than raising: a bad password is an expected outcome, not an error."""
    try:
        _hasher.verify(hashed, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def _ttl_for(kind: TokenKind) -> timedelta:
    if kind == "access":
        return timedelta(minutes=settings.jwt_access_ttl_minutes)
    if kind == "reset":
        # Short on purpose: a reset link sits in an inbox, which is exactly the
        # place a long-lived credential should not.
        return timedelta(minutes=settings.jwt_reset_ttl_minutes)
    if kind == "verify":
        # Not a credential - it proves an address, grants nothing, and is worth
        # nothing to a thief. It can afford to survive a weekend in an inbox.
        return timedelta(hours=settings.jwt_verify_ttl_hours)
    return timedelta(days=settings.jwt_refresh_ttl_days)


def create_token(subject: str, kind: TokenKind, token_version: int = 0) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "type": kind,
        "tv": token_version,
        "iat": now,
        "exp": now + _ttl_for(kind),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expect: TokenKind) -> dict[str, Any]:
    """Raises jwt.InvalidTokenError when the token is unusable for this purpose.

    The `type` claim is checked because a refresh token is long-lived: accepting
    one wherever an access token is expected would hand out a 30-day bearer.

    The `tv` claim is *returned*, not checked - only a database row knows the
    current version, and this module deliberately does not reach for one.
    Callers compare it against `user.token_version` via `token_is_current`.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expect:
        raise jwt.InvalidTokenError(f"expected a {expect} token")
    return payload


def token_is_current(payload: dict[str, Any], token_version: int) -> bool:
    """Whether this token predates the account's last invalidation.

    Tokens minted before the `tv` claim existed report 0, which matches the
    column default - so an existing session survives this change rather than
    every account being signed out by a deployment.
    """
    return int(payload.get("tv", 0)) == token_version
