"""Getting back in, and getting out for good.

Both flows make promises that are easy to get subtly wrong: a reset endpoint
that admits which addresses have accounts, and a delete that signs you out and
keeps your data. These check the promise, not just the status code.
"""

from __future__ import annotations

import jwt

from app.config import get_settings
from app.security import create_token

settings = get_settings()


async def _register(client, email: str = "rita@test.dev", password: str = "first-password-1"):
    created = await client.post(
        "/auth/register", json={"email": email, "password": password, "name": "Rita Cruz"}
    )
    assert created.status_code == 201
    return created.json()


async def test_forgot_password_never_reveals_whether_an_account_exists(client):
    await _register(client)
    known = await client.post("/auth/password/forgot", json={"email": "rita@test.dev"})
    unknown = await client.post("/auth/password/forgot", json={"email": "nobody@test.dev"})
    # Identical in every respect a caller can observe.
    assert known.status_code == unknown.status_code == 204
    assert known.content == unknown.content


async def test_reset_replaces_the_password_and_signs_in(client):
    tokens = await _register(client)
    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    reset_token = create_token(me.json()["id"], "reset")

    done = await client.post(
        "/auth/password/reset", json={"token": reset_token, "password": "second-password-1"}
    )
    assert done.status_code == 200
    assert done.json()["access_token"]

    stale = await client.post(
        "/auth/login", json={"email": "rita@test.dev", "password": "first-password-1"}
    )
    assert stale.status_code == 401

    fresh = await client.post(
        "/auth/login", json={"email": "rita@test.dev", "password": "second-password-1"}
    )
    assert fresh.status_code == 200


async def test_reset_refuses_anything_that_is_not_a_reset_token(client):
    tokens = await _register(client)
    for bad in ("nonsense", tokens["access_token"], tokens["refresh_token"]):
        # An access token is a valid JWT for a real user; accepting one here
        # would turn every leaked bearer into a password change.
        refused = await client.post(
            "/auth/password/reset", json={"token": bad, "password": "second-password-1"}
        )
        assert refused.status_code == 400, bad


async def test_reset_token_expires(client, monkeypatch):
    tokens = await _register(client)
    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    expired = jwt.encode(
        {"sub": me.json()["id"], "type": "reset", "exp": 0, "iat": 0},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    refused = await client.post(
        "/auth/password/reset", json={"token": expired, "password": "second-password-1"}
    )
    assert refused.status_code == 400


async def test_delete_account_actually_deletes_it(client, somedays):
    tokens = await _register(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Give the account something to lose, so a cascade failure would show.
    catalog = await client.get("/catalog")
    someday_id = catalog.json()[0]["id"]
    kept = await client.put(
        f"/somedays/{someday_id}/keep", json={"open_to": ["friends"], "party_as": ["duo"]}, headers=headers
    )
    assert kept.status_code == 201

    gone = await client.delete("/auth/me", headers=headers)
    assert gone.status_code == 204

    # The token is still cryptographically valid; the account behind it is not.
    assert (await client.get("/auth/me", headers=headers)).status_code == 401
    assert (
        await client.post(
            "/auth/login", json={"email": "rita@test.dev", "password": "first-password-1"}
        )
    ).status_code == 401
    # And the address is free again, which it would not be on a soft delete.
    again = await client.post(
        "/auth/register", json={"email": "rita@test.dev", "password": "third-password-1", "name": "Rita"}
    )
    assert again.status_code == 201
