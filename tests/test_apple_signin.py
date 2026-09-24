"""POST /auth/apple, and Apple token revocation on account deletion.

Route tests stub the verifier, as the Google ones do. The verifier itself is
exercised with a locally generated RSA key standing in for Apple's, because the
audience, issuer and email_verified checks are our decisions worth pinning.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.models import User
from app.routers import auth as auth_router
from app.services import apple_auth


@pytest.fixture
def apple_says(monkeypatch: pytest.MonkeyPatch):
    def _set(claims=None, *, error: str | None = None, refresh_token: str | None = None):
        def fake(_raw: str) -> dict[str, str]:
            if error is not None:
                raise apple_auth.AppleAuthError(error)
            return claims

        async def fake_exchange(_code: str) -> str | None:
            return refresh_token

        monkeypatch.setattr(auth_router.apple_auth, "verify_identity_token", fake)
        monkeypatch.setattr(auth_router.apple_auth, "exchange_code", fake_exchange)

    return _set


async def test_first_apple_sign_in_creates_an_account(client, apple_says):
    email = f"new-{uuid.uuid4().hex[:8]}@privaterelay.appleid.com"
    apple_says({"sub": "apple-sub-1", "email": email}, refresh_token="rt-1")

    response = await client.post(
        "/auth/apple",
        json={"identity_token": "t", "authorization_code": "c", "name": "Bea"},
    )

    assert response.status_code == 200, response.text
    async with client.sessionmaker() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.apple_sub == "apple-sub-1"
    assert user.apple_refresh_token == "rt-1"
    assert user.name == "Bea"
    assert user.password_hash is None
    assert user.email_verified is True


async def test_returning_apple_user_is_the_same_account(client, apple_says):
    email = f"again-{uuid.uuid4().hex[:8]}@icloud.com"
    apple_says({"sub": "apple-sub-2", "email": email})

    assert (await client.post("/auth/apple", json={"identity_token": "a"})).status_code == 200
    assert (await client.post("/auth/apple", json={"identity_token": "b"})).status_code == 200

    async with client.sessionmaker() as session:
        rows = (await session.execute(select(User.id).where(User.email == email))).all()
    assert len(rows) == 1


async def test_apple_links_to_an_existing_password_account(client, apple_says):
    email = f"both-{uuid.uuid4().hex[:8]}@icloud.com"
    registered = await client.post(
        "/auth/register", json={"email": email, "password": "long-enough-pass", "name": "Bea"}
    )
    assert registered.status_code == 201

    apple_says({"sub": "apple-sub-3", "email": email})
    assert (await client.post("/auth/apple", json={"identity_token": "c"})).status_code == 200

    async with client.sessionmaker() as session:
        rows = (await session.execute(select(User).where(User.email == email))).scalars().all()
    assert len(rows) == 1
    assert rows[0].apple_sub == "apple-sub-3"
    assert rows[0].password_hash is not None


async def test_a_token_apple_rejects_is_a_401(client, apple_says):
    apple_says(error="Signature verification failed")
    response = await client.post("/auth/apple", json={"identity_token": "rubbish"})
    assert response.status_code == 401
    assert "Signature" not in response.text


async def test_deleting_an_apple_account_revokes_its_token(client, apple_says, monkeypatch):
    apple_says({"sub": "apple-sub-4", "email": f"del-{uuid.uuid4().hex[:8]}@icloud.com"}, refresh_token="rt-4")
    pair = (await client.post("/auth/apple", json={"identity_token": "d", "authorization_code": "c"})).json()

    revoked: list[str] = []

    async def fake_revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(auth_router.apple_auth, "revoke", fake_revoke)
    response = await client.delete(
        "/auth/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )

    assert response.status_code == 204
    assert revoked == ["rt-4"]


# --- the verifier, against a stand-in for Apple's signing key ---

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(**overrides) -> str:
    claims = {
        "iss": apple_auth.ISSUER,
        "aud": "com.sumnora.gimik",
        "sub": "apple-sub",
        "email": "someone@icloud.com",
        "email_verified": "true",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    } | overrides
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "k"})


@pytest.fixture
def apple_key(monkeypatch: pytest.MonkeyPatch):
    class _Signing:
        key = _KEY.public_key()

    monkeypatch.setattr(apple_auth._jwks, "get_signing_key_from_jwt", lambda _raw: _Signing())


def test_verifier_accepts_a_good_token(apple_key):
    assert apple_auth.verify_identity_token(_token()) == {
        "sub": "apple-sub",
        "email": "someone@icloud.com",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "com.someone.else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 600},
        {"email_verified": "false"},
    ],
)
def test_verifier_refuses_bad_tokens(apple_key, overrides):
    with pytest.raises(apple_auth.AppleAuthError):
        apple_auth.verify_identity_token(_token(**overrides))
