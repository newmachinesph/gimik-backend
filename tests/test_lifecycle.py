"""The whole journey, end to end, over the wire:

want -> be seen -> invited -> talk -> lock -> done -> record.

Plus the social edges around it: friendship is mutual, hiding is silent,
visibility gates who can see and invite.
"""

from __future__ import annotations


async def test_register_login_me(client):
    created = await client.post(
        "/auth/register",
        json={"email": "bea@test.dev", "password": "long-enough-pass", "name": "Bea"},
    )
    assert created.status_code == 201

    dup = await client.post(
        "/auth/register",
        json={"email": "bea@test.dev", "password": "long-enough-pass", "name": "Bea"},
    )
    assert dup.status_code == 409

    login = await client.post(
        "/auth/login", json={"email": "bea@test.dev", "password": "long-enough-pass"}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = await client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["name"] == "Bea"
    assert me.json()["handle"] == "bea"

    wrong = await client.post(
        "/auth/login", json={"email": "bea@test.dev", "password": "wrong-password!"}
    )
    assert wrong.status_code == 401


async def test_catalog_carries_minio_artwork(client, somedays):
    listing = await client.get("/catalog")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    by_slug = {row["slug"]: row for row in rows}
    # The artwork URL points into the public catalog bucket the v1 stack seeded.
    assert by_slug["dive-a-coral-reef"]["artwork_url"].endswith(
        "/gora-catalog/catalog/dive-a-coral-reef.webp"
    )


async def test_keep_pass_visibility(client, somedays, make_user):
    me = await make_user("Bea")

    kept = await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["friends"], "party_as": ["duo"]},
        headers=me,
    )
    assert kept.status_code == 201

    mine = await client.get("/me/somedays", headers=me)
    assert [row["someday"]["slug"] for row in mine.json()] == ["dive-a-coral-reef"]

    changed = await client.put(
        f"/somedays/{somedays['reef']}/visibility",
        json={"open_to": ["friends", "everyone"], "party_as": ["duo", "group"]},
        headers=me,
    )
    assert changed.status_code == 200
    assert set(changed.json()["open_to"]) == {"friends", "everyone"}

    # Passing removes the keep; they are exclusive answers to one card.
    passed = await client.put(f"/somedays/{somedays['reef']}/pass", headers=me)
    assert passed.status_code == 204
    assert (await client.get("/me/somedays", headers=me)).json() == []

    bad = await client.put(
        f"/somedays/{somedays['lights']}/keep",
        json={"open_to": ["the-world"], "party_as": ["duo"]},
        headers=me,
    )
    assert bad.status_code == 422


async def test_friendship_is_mutual(client, make_user):
    bea = await make_user("Bea")
    mara = await make_user("Mara")
    mara_id = (await client.get("/auth/me", headers=mara)).json()["id"]
    bea_id = (await client.get("/auth/me", headers=bea)).json()["id"]

    requested = await client.post(f"/friends/{mara_id}/request", headers=bea)
    assert requested.status_code == 201
    assert requested.json()["status"] == "pending"

    # Not friends yet, in either direction.
    assert (await client.get("/friends", headers=bea)).json()["friends"] == []
    incoming = (await client.get("/friends", headers=mara)).json()
    assert [row["from_user"]["id"] for row in incoming["requests"]] == [bea_id]

    accepted = await client.post(f"/friends/{bea_id}/accept", headers=mara)
    assert accepted.status_code == 204
    assert [f["id"] for f in (await client.get("/friends", headers=bea)).json()["friends"]] == [
        mara_id
    ]
    assert [f["id"] for f in (await client.get("/friends", headers=mara)).json()["friends"]] == [
        bea_id
    ]


async def test_visibility_gates_holders_and_invites(client, somedays, make_user):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    stranger = await make_user("Jon", city="Makati")
    mara_id = (await client.get("/auth/me", headers=mara)).json()["id"]

    # Mara keeps the reef, open to Everyone in her city, as a duo.
    await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["everyone"], "party_as": ["duo"]},
        headers=mara,
    )

    # Bea (same city) sees Mara among the holders; Jon (other city) does not.
    bea_view = await client.get(f"/somedays/{somedays['reef']}/holders", headers=bea)
    assert [holder["id"] for holder in bea_view.json()] == [mara_id]
    jon_view = await client.get(f"/somedays/{somedays['reef']}/holders", headers=stranger)
    assert jon_view.json() == []

    # And the same rule gates inviting.
    ok = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=bea,
    )
    assert ok.status_code == 201
    blocked = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=stranger,
    )
    assert blocked.status_code == 403
    # The shape is the holder's choice too: group was not offered.
    wrong_shape = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "group"},
        headers=bea,
    )
    assert wrong_shape.status_code == 409


async def test_invite_accept_opens_pinned_thread(client, somedays, make_user):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    mara_id = (await client.get("/auth/me", headers=mara)).json()["id"]

    await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["everyone"], "party_as": ["duo"]},
        headers=mara,
    )
    invite = (
        await client.post(
            "/invites",
            json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
            headers=bea,
        )
    ).json()

    waiting = (await client.get("/invites", headers=mara)).json()
    assert [row["id"] for row in waiting] == [invite["id"]]
    assert waiting[0]["someday"]["slug"] == "dive-a-coral-reef"

    accepted = await client.post(f"/invites/{invite['id']}/accept", headers=mara)
    assert accepted.status_code == 200
    thread_id = accepted.json()["thread_id"]

    detail = (await client.get(f"/threads/{thread_id}", headers=mara)).json()
    assert detail["kind"] == "solo"
    assert detail["someday"]["slug"] == "dive-a-coral-reef"  # pinned

    # Talk, with a date proposal attached; the sender's copy is read, Bea's is not.
    sent = await client.post(
        f"/threads/{thread_id}/messages",
        json={
            "body": "How about Saturday?",
            "attachment": {"type": "date", "ref": "2026-09-05", "label": "Sat, Sep 5"},
        },
        headers=mara,
    )
    assert sent.status_code == 201
    bea_threads = (await client.get("/threads", headers=bea)).json()
    assert bea_threads[0]["unread"] is True
    await client.post(f"/threads/{thread_id}/read", headers=bea)
    assert (await client.get("/threads", headers=bea)).json()[0]["unread"] is False


async def test_lock_done_journal_record(client, somedays, make_user):
    bea = await make_user("Bea")
    mara = await make_user("Mara")
    mara_id = (await client.get("/auth/me", headers=mara)).json()["id"]

    locked = await client.post(
        "/plans",
        json={
            "someday_id": somedays["reef"],
            "date": "2026-09-05",
            "time_slot": "Morning",
            "people": [mara_id],
        },
        headers=bea,
    )
    assert locked.status_code == 201
    plan = locked.json()
    assert plan["thread_id"] is not None
    assert {person["id"] for person in plan["people"]} == {
        (await client.get("/auth/me", headers=bea)).json()["id"],
        mara_id,
    }

    # Locking implied keeping.
    assert [row["someday"]["slug"] for row in (await client.get("/me/somedays", headers=bea)).json()] == [
        "dive-a-coral-reef"
    ]
    # A second live lock of the same someday is refused.
    again = await client.post(
        "/plans", json={"someday_id": somedays["reef"], "date": "2026-09-06"}, headers=bea
    )
    assert again.status_code == 409

    # Both people are in the plan thread.
    mara_threads = (await client.get("/threads", headers=mara)).json()
    assert [thread["kind"] for thread in mara_threads] == ["plan"]

    done = await client.post(f"/plans/{plan['id']}/done", headers=bea)
    assert done.status_code == 200
    assert done.json()["done_at"] is not None

    entry = await client.post(
        f"/somedays/{somedays['reef']}/journal",
        json={"body": "Colder than expected. Would do it again tomorrow."},
        headers=bea,
    )
    assert entry.status_code == 201
    entries = (await client.get(f"/somedays/{somedays['reef']}/journal", headers=bea)).json()
    assert len(entries) == 1

    empty = await client.post(
        f"/somedays/{somedays['reef']}/journal", json={"body": "   "}, headers=bea
    )
    assert empty.status_code == 422


async def test_unlock_returns_to_waiting(client, somedays, make_user):
    bea = await make_user("Bea")
    plan = (
        await client.post(
            "/plans", json={"someday_id": somedays["reef"], "date": "2026-09-05"}, headers=bea
        )
    ).json()
    gone = await client.delete(f"/plans/{plan['id']}", headers=bea)
    assert gone.status_code == 204
    assert (await client.get("/plans", headers=bea)).json() == []
    # The keep stays: only the date went away.
    assert len((await client.get("/me/somedays", headers=bea)).json()) == 1


async def test_groups_membership_not_friendship(client, somedays, make_user, make_plus_user):
    # Creating a group is a Plus feature; joining one is not, and Theo stays
    # free for the whole test to prove it.
    bea = await make_plus_user("Bea")
    theo = await make_user("Theo")
    theo_id = (await client.get("/auth/me", headers=theo)).json()["id"]

    group = (await client.post("/groups", json={"name": "Saturday People"}, headers=bea)).json()
    joined = await client.post(f"/groups/join/{group['invite_token']}", headers=theo)
    assert joined.status_code == 200
    assert len(joined.json()["members"]) == 2

    # Theo keeps the aurora open to Groups; Bea can invite despite no friendship.
    await client.put(
        f"/somedays/{somedays['lights']}/keep",
        json={"open_to": ["groups"], "party_as": ["group"]},
        headers=theo,
    )
    invited = await client.post(
        "/invites",
        json={"to_user_id": theo_id, "someday_id": somedays["lights"], "party": "group"},
        headers=bea,
    )
    assert invited.status_code == 201

    # Leaving the group closes that door.
    await client.delete(f"/groups/{group['id']}/membership", headers=theo)
    blocked = await client.post(
        "/invites",
        json={"to_user_id": theo_id, "someday_id": somedays["lights"], "party": "group"},
        headers=bea,
    )
    assert blocked.status_code == 403


async def test_hide_is_quiet_and_reversible(client, somedays, make_user):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    bea_id = (await client.get("/auth/me", headers=bea)).json()["id"]
    mara_id = (await client.get("/auth/me", headers=mara)).json()["id"]

    await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["everyone"], "party_as": ["duo"]},
        headers=mara,
    )
    await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=bea,
    )
    assert len((await client.get("/invites", headers=mara)).json()) == 1

    # Hiding Bea kills her live invites and blocks new ones - with a 404, not an
    # explanation (nobody is told).
    await client.put(f"/hidden/{bea_id}", headers=mara)
    assert (await client.get("/invites", headers=mara)).json() == []
    blocked = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=bea,
    )
    assert blocked.status_code == 404

    # Reversible in Settings.
    await client.delete(f"/hidden/{bea_id}", headers=mara)
    ok = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=bea,
    )
    assert ok.status_code == 201


async def test_person_page_and_media(client, somedays, make_user):
    bea = await make_user("Bea")
    mara = await make_user("Mara")
    mara_handle = (await client.get("/auth/me", headers=mara)).json()["handle"]

    await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["friends"], "party_as": ["duo"]},
        headers=mara,
    )
    await client.put(
        f"/somedays/{somedays['reef']}/keep",
        json={"open_to": ["friends"], "party_as": ["duo"]},
        headers=bea,
    )

    person = (await client.get(f"/people/{mara_handle}", headers=bea)).json()
    assert person["name"] == "Mara"
    assert [row["slug"] for row in person["in_common"]] == ["dive-a-coral-reef"]
    assert person["is_friend"] is False

    # Media: presign inside the caller's namespace; a foreign key is rejected.
    me = (await client.get("/auth/me", headers=bea)).json()
    presigned = (
        await client.post(
            "/media/presign",
            json={"filename": "face.jpg", "content_type": "image/jpeg"},
            headers=bea,
        )
    ).json()
    assert presigned["key"].startswith(me["id"])
    added = await client.post(
        "/media/photos",
        json={"key": presigned["key"], "upload_url": ""},
        headers=bea,
    )
    assert added.status_code == 201
    stolen = await client.post(
        "/media/photos", json={"key": "someone-else/x.jpg", "upload_url": ""}, headers=bea
    )
    assert stolen.status_code == 403
