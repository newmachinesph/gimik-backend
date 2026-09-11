"""POST /auth/google - the three arrival cases, and the ways in that must fail.

Google's own verification is stubbed: minting a real ID token would need a real
Google key, and what is worth testing here is not whether google-auth can check
a signature but what this codebase does with the claims once it trusts them.
The one exception is the email_verified rule, which is our decision rather than
the library's, so it is exercised against the real verifier's contract.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import User
from app.routers import auth as auth_router
from app.services import google_auth


def _claims(email: str, sub: str, name: str = "Bea Cruz") -> dict[str, str]:
    return {"sub": sub, "email": email, "name": name}


@pytest.fixture
def google_says(monkeypatch: pytest.MonkeyPatch):
    """Make the verifier return these claims, or raise, without touching Google."""

    def _set(claims=None, *, error: str | None = None):
        def fake(_raw: str) -> dict[str, str]:
            if error is not None:
                raise google_auth.GoogleAuthError(error)
            return claims

        monkeypatch.setattr(auth_router.google_auth, "verify_id_token", fake)

    return _set


async def test_first_google_sign_in_creates_an_account(client, google_says):
    email = f"new-{uuid.uuid4().hex[:8]}@gmail.com"
    google_says(_claims(email, sub="google-sub-1"))

    response = await client.post("/auth/google", json={"id_token": "whatever"})

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]
    async with client.sessionmaker() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.google_sub == "google-sub-1"
    # Google proved the address, so no verification mail is owed.
    assert user.email_verified is True
    # The account has no password, and that is representable rather than faked.
    assert user.password_hash is None


async def test_returning_google_user_is_the_same_account(client, google_says):
    email = f"again-{uuid.uuid4().hex[:8]}@gmail.com"
    google_says(_claims(email, sub="google-sub-2"))

    first = await client.post("/auth/google", json={"id_token": "a"})
    second = await client.post("/auth/google", json={"id_token": "b"})

    assert first.status_code == 200 and second.status_code == 200
    async with client.sessionmaker() as session:
        count = await session.scalar(
            select(User).where(User.google_sub == "google-sub-2").with_only_columns(User.id)
        )
        rows = (await session.execute(select(User.id).where(User.email == email))).all()
    assert count is not None
    assert len(rows) == 1, "a second sign-in must not make a second account"


async def test_google_links_to_an_existing_password_account(client, google_says):
    email = f"both-{uuid.uuid4().hex[:8]}@gmail.com"
    registered = await client.post(
        "/auth/register",
        json={"email": email, "password": "long-enough-pass", "name": "Bea"},
    )
    assert registered.status_code == 201

    google_says(_claims(email, sub="google-sub-3"))
    response = await client.post("/auth/google", json={"id_token": "c"})

    assert response.status_code == 200, response.text
    async with client.sessionmaker() as session:
        rows = (await session.execute(select(User).where(User.email == email))).scalars().all()
    assert len(rows) == 1, "linking, not duplicating"
    assert rows[0].google_sub == "google-sub-3"
    assert rows[0].email_verified is True
    # The password still exists: linking Google must not lock the old way out.
    assert rows[0].password_hash is not None

    still_works = await client.post(
        "/auth/login", json={"email": email, "password": "long-enough-pass"}
    )
    assert still_works.status_code == 200


async def test_a_token_google_rejects_is_a_401(client, google_says):
    google_says(error="Wrong number of segments")
    response = await client.post("/auth/google", json={"id_token": "rubbish"})
    assert response.status_code == 401
    # The reason stays in the log, not the response.
    assert "segments" not in response.text


async def test_password_login_fails_for_a_google_only_account(client, google_says):
    email = f"nopw-{uuid.uuid4().hex[:8]}@gmail.com"
    google_says(_claims(email, sub="google-sub-4"))
    assert (await client.post("/auth/google", json={"id_token": "d"})).status_code == 200

    response = await client.post("/auth/login", json={"email": email, "password": "guessing"})

    assert response.status_code == 401
    assert response.json()["detail"] == "That email and password do not match"


def test_verifier_refuses_an_unverified_address(monkeypatch: pytest.MonkeyPatch):
    """Our rule, not the library's: linking by email is only safe once Google
    has confirmed the address, so an unverified one is refused outright."""
    monkeypatch.setattr(
        google_auth.google_id_token,
        "verify_oauth2_token",
        lambda *a, **k: {"sub": "x", "email": "someone@gmail.com", "email_verified": False},
    )
    with pytest.raises(google_auth.GoogleAuthError, match="not verified"):
        google_auth.verify_id_token("token")
