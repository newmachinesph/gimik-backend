"""Sign in with Apple: verify the identity token, and keep what deletion needs.

The app sends the identity token Apple minted for it. Like the Google verifier,
this module decides whether that token is real, fresh and addressed to us: the
signature against Apple's current keys, the issuer, expiry, and `aud` equal to
our bundle id. Everything downstream trusts the claims it returns.

The second half exists because App Store rules require revoking a user's Apple
tokens when they delete their account. That needs a refresh token, which only
comes from exchanging the one-time authorization code, which needs a client
secret signed with our Sign in with Apple key. Without that key configured,
sign-in still works and exchange/revoke are skipped.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt

from app.config import get_settings

log = logging.getLogger("gimik.apple_auth")

ISSUER = "https://appleid.apple.com"
_TOKEN_URL = f"{ISSUER}/auth/token"
_REVOKE_URL = f"{ISSUER}/auth/revoke"

# PyJWKClient caches Apple's keys and refetches on an unknown `kid`.
_jwks = jwt.PyJWKClient(f"{ISSUER}/auth/keys", cache_keys=True)

_CLOCK_SKEW_SECONDS = 10


class AppleAuthError(Exception):
    """The token is not one we can act on. The reason is for the log, not the caller."""


def verify_identity_token(raw_token: str) -> dict[str, Any]:
    """Return the verified claims, or raise AppleAuthError.

    Blocking: it may fetch Apple's signing keys. Call it off the event loop.
    """
    try:
        key = _jwks.get_signing_key_from_jwt(raw_token).key
        claims = jwt.decode(
            raw_token,
            key,
            algorithms=["RS256"],
            audience=get_settings().apple_bundle_id,
            issuer=ISSUER,
            leeway=_CLOCK_SKEW_SECONDS,
        )
    except jwt.PyJWTError as exc:
        raise AppleAuthError(str(exc)) from exc

    subject = claims.get("sub")
    if not subject:
        raise AppleAuthError("Token carries no subject")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise AppleAuthError("Token carries no email")

    # Apple sends this as a bool or the string "true". Same rule as Google:
    # linking by email is only safe for an address the provider has proved.
    if str(claims.get("email_verified")).lower() != "true":
        raise AppleAuthError("Apple has not verified that address")

    return {"sub": subject, "email": email}


def _client_secret() -> str:
    settings = get_settings()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.apple_team_id,
            "iat": now,
            "exp": now + 300,
            "aud": ISSUER,
            "sub": settings.apple_bundle_id,
        },
        # Env vars often carry a PEM with literal "\n".
        settings.apple_private_key.replace("\\n", "\n"),
        algorithm="ES256",
        headers={"kid": settings.apple_key_id},
    )


async def exchange_code(code: str) -> str | None:
    """Swap a one-time authorization code for a refresh token. None if unavailable."""
    settings = get_settings()
    if not settings.apple_revocation_enabled:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": settings.apple_bundle_id,
                    "client_secret": _client_secret(),
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
        response.raise_for_status()
        return response.json().get("refresh_token")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Apple code exchange failed: %s", exc)
        return None


async def revoke(refresh_token: str) -> None:
    """Revoke the user's Apple tokens. Raises on failure; the caller decides."""
    settings = get_settings()
    if not settings.apple_revocation_enabled:
        raise AppleAuthError("Apple revocation is not configured on this server")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            _REVOKE_URL,
            data={
                "client_id": settings.apple_bundle_id,
                "client_secret": _client_secret(),
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
        )
    response.raise_for_status()
