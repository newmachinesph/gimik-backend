"""Ending a session, and proving an address.

These cover the half of authentication a stateless scheme cannot do by itself.
Every one of them failed before `users.token_version` existed: a refresh token
survived sign-out, survived a password change, and a reset link worked as many
times as anyone cared to replay it inside its half hour.
"""

from __future__ import annotations

import jwt

from app.config import get_settings
from app.security import create_token

settings = get_settings()


async def _register(client, email="sess@test.dev", password="long-enough-pass"):
    response = await client.post(
        "/auth/register", json={"email": email, "password": password, "name": "Bea"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_logout_kills_the_refresh_token_not_just_the_device(client):
    """The point of the column: reaching a token that is no longer on this phone."""
    pair = await _register(client)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    assert (await client.get("/auth/me", headers=headers)).status_code == 200
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204

    # The access token is refused...
    assert (await client.get("/auth/me", headers=headers)).status_code == 401
    # ...and so is the thirty-day refresh token, which is the one that mattered.
    refreshed = await client.post(
        "/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_changing_the_password_signs_out_every_other_device(client):
    pair = await _register(client)
    old = {"Authorization": f"Bearer {pair['access_token']}"}

    me = (await client.get("/auth/me", headers=old)).json()
    reset = await client.post(
        "/auth/password/reset",
        json={
            "token": create_token(me["id"], "reset", 0),
            "password": "a-brand-new-password",
        },
    )
    assert reset.status_code == 200

    # The session that existed before the change is gone.
    assert (await client.get("/auth/me", headers=old)).status_code == 401
    # The pair handed back by the reset works.
    fresh = {"Authorization": f"Bearer {reset.json()['access_token']}"}
    assert (await client.get("/auth/me", headers=fresh)).status_code == 200


async def test_a_reset_link_cannot_be_replayed(client):
    """It used to work for its whole thirty minutes, as many times as you liked."""
    pair = await _register(client)
    me = (await client.get("/auth/me", headers={"Authorization": f"Bearer {pair['access_token']}"})).json()
    link = create_token(me["id"], "reset", 0)

    first = await client.post("/auth/password/reset", json={"token": link, "password": "first-new-password"})
    assert first.status_code == 200

    second = await client.post("/auth/password/reset", json={"token": link, "password": "attackers-password"})
    assert second.status_code == 400
    # And the password the replay tried to set was never applied.
    assert (
        await client.post("/auth/login", json={"email": "sess@test.dev", "password": "attackers-password"})
    ).status_code == 401
    assert (
        await client.post("/auth/login", json={"email": "sess@test.dev", "password": "first-new-password"})
    ).status_code == 200


async def test_a_token_from_another_secret_is_refused(client):
    forged = jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000000", "type": "access", "tv": 0},
        "not-the-signing-secret-but-long-enough-for-sha256",
        algorithm="HS256",
    )
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_email_starts_unverified_and_a_link_proves_it(client):
    pair = await _register(client)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    me = (await client.get("/auth/me", headers=headers)).json()
    assert me["email_verified"] is False
    # Unverified is not a gate: the account works completely.
    assert (await client.patch("/auth/me", json={"city": "Makati"}, headers=headers)).status_code == 200

    verified = await client.post(
        "/auth/email/verify", json={"token": create_token(me["id"], "verify", 0)}
    )
    assert verified.status_code == 200
    assert verified.json()["email_verified"] is True

    # Mail clients prefetch links; the second visit must not read as a failure.
    again = await client.post(
        "/auth/email/verify", json={"token": create_token(me["id"], "verify", 0)}
    )
    assert again.status_code == 200


async def test_verification_refuses_a_token_of_the_wrong_kind(client):
    """An access token must not double as proof of an address."""
    pair = await _register(client)
    response = await client.post("/auth/email/verify", json={"token": pair["access_token"]})
    assert response.status_code == 400


async def test_resend_never_reveals_whether_an_account_exists(client):
    await _register(client)
    for email in ("sess@test.dev", "nobody@test.dev"):
        response = await client.post("/auth/email/resend", json={"email": email})
        assert response.status_code == 204
