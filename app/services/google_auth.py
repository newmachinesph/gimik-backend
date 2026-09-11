"""Verifying a Google ID token, and nothing else.

The app never sends us a password for a Google account. It sends the ID token
Google minted for it, and this module decides whether that token is real, fresh,
and addressed to us. Everything downstream trusts the claims it returns.

Why the official library rather than decoding the JWT ourselves: the checks that
matter here are the ones that are easy to forget - that the signature verifies
against Google's *current* signing keys, that the issuer really is Google, that
the token has not expired, and that its `aud` names one of our clients. Getting
any one of them wrong turns this endpoint into "sign in as anybody".
"""

from __future__ import annotations

import logging
from typing import Any

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import get_settings

log = logging.getLogger("gimik.google_auth")

# Google's certs are fetched over HTTP and cached on the transport, so this is
# built once rather than per request.
_transport = google_requests.Request()

# Small tolerance for a phone whose clock drifts. Google's own samples use this.
_CLOCK_SKEW_SECONDS = 10


class GoogleAuthError(Exception):
    """The token is not one we can act on. The reason is for the log, not the caller."""


def verify_id_token(raw_token: str) -> dict[str, Any]:
    """Return the verified claims, or raise GoogleAuthError.

    Blocking: it may fetch Google's signing keys. Call it off the event loop.
    """
    audiences = get_settings().google_audiences
    if not audiences:
        raise GoogleAuthError("Google sign-in is not configured on this server")

    try:
        claims = google_id_token.verify_oauth2_token(
            raw_token,
            _transport,
            audience=audiences,
            clock_skew_in_seconds=_CLOCK_SKEW_SECONDS,
        )
    except ValueError as exc:
        # Bad signature, wrong audience, expired, malformed - all the same to us.
        raise GoogleAuthError(str(exc)) from exc

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Token carries no email")

    # The one check the library does not make for us, and the one that decides
    # whether linking by email is safe. An unverified address proves nothing:
    # anyone can put someone else's email on a Google account, and honouring it
    # would hand them that person's account here.
    if not claims.get("email_verified"):
        raise GoogleAuthError("Google has not verified that address")

    subject = claims.get("sub")
    if not subject:
        raise GoogleAuthError("Token carries no subject")

    return {
        "sub": subject,
        "email": email,
        "name": (claims.get("name") or claims.get("given_name") or "").strip(),
    }
