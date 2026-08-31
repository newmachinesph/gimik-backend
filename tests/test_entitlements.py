"""What a free account gets, enforced where it has to be: on this side.

The app knows the tier too and hides the buttons, but a hidden button is a
suggestion. These tests send the request the hidden button would have sent.
"""

from __future__ import annotations

from app.entitlements import FREE_INVITES_PER_WEEK, FREE_PHOTOS, PLUS_PHOTOS


async def _claim_photo(client, headers, index: int):
    presigned = await client.post(
        "/media/presign",
        json={"filename": f"face{index}.jpg", "content_type": "image/jpeg"},
        headers=headers,
    )
    assert presigned.status_code == 200, presigned.text
    return await client.post(
        "/media/photos", json={"key": presigned.json()["key"]}, headers=headers
    )


async def test_free_accounts_stop_at_the_free_photo_allowance(client, make_user):
    me = await make_user("Bea")
    for index in range(FREE_PHOTOS):
        assert (await _claim_photo(client, me, index)).status_code == 201

    refused = await _claim_photo(client, me, FREE_PHOTOS)
    # 402, not 409: more is purchasable, so the app opens the paywall.
    assert refused.status_code == 402


async def test_plus_accounts_get_the_full_allowance(client, make_plus_user):
    me = await make_plus_user("Cy")
    for index in range(PLUS_PHOTOS):
        assert (await _claim_photo(client, me, index)).status_code == 201

    # 409 this time: the ceiling is the ceiling, and no purchase moves it.
    refused = await _claim_photo(client, me, PLUS_PHOTOS)
    assert refused.status_code == 409


async def _send_invite(client, sender, target_id: str, someday_id: str):
    return await client.post(
        "/invites",
        json={"to_user_id": target_id, "someday_id": someday_id, "party": "duo"},
        headers=sender,
    )


async def test_free_accounts_have_a_weekly_invite_ceiling(client, somedays, make_user):
    """The sharpest lever in the product - and never applied to receiving."""
    bea = await make_user("Bea", city="Makati")

    targets = []
    for index in range(FREE_INVITES_PER_WEEK + 1):
        friend = await make_user(f"Friend{index}", city="Makati")
        friend_id = (await client.get("/auth/me", headers=friend)).json()["id"]
        # Open to everyone, so visibility is never what refuses the invite.
        keep = await client.put(
            f"/somedays/{somedays['reef']}/keep",
            json={"open_to": ["everyone"], "party_as": ["duo"]},
            headers=friend,
        )
        assert keep.status_code == 201, keep.text
        targets.append(friend_id)

    for index in range(FREE_INVITES_PER_WEEK):
        sent = await _send_invite(client, bea, targets[index], somedays["reef"])
        assert sent.status_code == 201, sent.text

    over = await _send_invite(client, bea, targets[FREE_INVITES_PER_WEEK], somedays["reef"])
    assert over.status_code == 402


async def test_plus_accounts_are_not_capped(client, somedays, make_user, make_plus_user):
    bea = await make_plus_user("Bea", city="Makati")

    for index in range(FREE_INVITES_PER_WEEK + 1):
        friend = await make_user(f"Friend{index}", city="Makati")
        friend_id = (await client.get("/auth/me", headers=friend)).json()["id"]
        assert (
            await client.put(
                f"/somedays/{somedays['reef']}/keep",
                json={"open_to": ["everyone"], "party_as": ["duo"]},
                headers=friend,
            )
        ).status_code == 201
        sent = await _send_invite(client, bea, friend_id, somedays["reef"])
        assert sent.status_code == 201, sent.text


async def test_profile_reports_the_tier(client, make_user, make_plus_user):
    free = await client.get("/auth/me", headers=await make_user("Bea"))
    assert free.json()["tier"] == "free"
    assert free.json()["plus_until"] is None

    paid = await client.get("/auth/me", headers=await make_plus_user("Cy"))
    assert paid.json()["tier"] == "plus"
    assert paid.json()["plus_until"] is not None
