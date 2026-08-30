"""Password hashing and token issuing, unchanged in spirit from v1.

Argon2id rather than bcrypt: bcrypt silently truncates at 72 bytes, so two long
passphrases sharing a prefix authenticate each other.
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

TokenKind = Literal["access", "refresh", "reset"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """False rather than raising: a bad password is an expected outcome, not an error."""
    try:
        _hasher.verify(hashed, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_token(subject: str, kind: TokenKind) -> str:
    now = datetime.now(UTC)
    if kind == "access":
        ttl = timedelta(minutes=settings.jwt_access_ttl_minutes)
    elif kind == "reset":
        # Short on purpose: a reset link sits in an inbox, which is exactly the
        # place a long-lived credential should not.
        ttl = timedelta(minutes=settings.jwt_reset_ttl_minutes)
    else:
        ttl = timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub": subject,
        "type": kind,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expect: TokenKind) -> dict[str, Any]:
    """Raises jwt.InvalidTokenError when the token is unusable for this purpose.

    The `type` claim is checked because a refresh token is long-lived: accepting
    one wherever an access token is expected would hand out a 30-day bearer.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expect:
        raise jwt.InvalidTokenError(f"expected a {expect} token")
    return payload
