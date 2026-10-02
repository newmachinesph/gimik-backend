"""App Store guideline 1.2: filter, flag, block, and act on it.

Each test sends the request the app would send. The moderation inbox is a list
here, so "the developer was told" is something a test can read.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Report, Someday, User
from app.services import email, moderation
from scripts import moderate

OPEN = {"open_to": ["everyone"], "party_as": ["duo"]}


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(
        email,
        "send_moderation_alert",
        lambda tasks, *, subject, text: sent.append({"subject": subject, "text": text}),
    )
    return sent


async def _id(client, headers) -> str:
    return (await client.get("/auth/me", headers=headers)).json()["id"]


async def _write(client, headers, title: str) -> str:
    made = await client.post(
        "/somedays", json={"title": title, "category": "Food & Drink", **OPEN}, headers=headers
    )
    assert made.status_code == 201, made.text
    return made.json()["someday"]["id"]


async def _deck(client, headers) -> set[str]:
    items = (await client.get("/feed", headers=headers)).json()["items"]
    return {item["someday"]["id"] for item in items}


async def _pair(client, make_user):
    """Bea and Mara in one city, with their ids."""
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    return bea, mara, await _id(client, bea), await _id(client, mara)


def test_filter_knows_abuse_from_ordinary_words():
    for foul in ["you are a f4ggot", "what the fuuuuck", "Putang ina mo", "fuckyou", "fück", "fu​ck"]:
        assert moderation.objectionable(foul), foul
    # Messages are held only to the severe list.
    assert not moderation.objectionable("fuck yes, Saturday", public=False)
    assert moderation.objectionable("kys", public=False)
    # Words and names that merely contain or resemble a blocked one.
    for clean in [
        "Scunthorpe cocktail class",
        "magna cum laude",
        "paki-abot",
        "shiitake",
        "Adopt a Maine Coon cat",
        "Dick Whittington",
        "watch blue tits at dawn",
        "spic and span",
        "Titi",
        "5 a.m. hike, 1 day",
        "",
    ]:
        assert not moderation.objectionable(clean), clean


async def test_objectionable_text_is_refused_where_strangers_read_it(client, make_user):
    bea = await make_user("Bea", city="Cebu City")
    refused = await client.post(
        "/somedays",
        json={"title": "Fuck around downtown", "category": "Food & Drink", **OPEN},
        headers=bea,
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == moderation.REJECTED
    for patch in [{"bio": "total slut"}, {"city": "Shit City"}]:
        assert (await client.patch("/auth/me", json=patch, headers=bea)).status_code == 422
    named = await client.post(
        "/auth/register",
        json={"email": "x@test.dev", "password": "long-enough-pass", "name": "Nigger"},
    )
    assert named.status_code == 422
    await _write(client, bea, "Find the bakery lola talks about")


async def test_messages_and_their_attachments_are_screened(client, make_user):
    _, mara, bea_id, _ = await _pair(client, make_user)
    thread = (await client.post(f"/threads/solo/{bea_id}", headers=mara)).json()["id"]
    for body in [
        {"body": "kys"},
        {"body": "this one?", "attachment": {"type": "place", "ref": "x", "label": "faggot bar"}},
    ]:
        sent = await client.post(f"/threads/{thread}/messages", json=body, headers=mara)
        assert sent.status_code == 422, body
    # Ordinary swearing between two people who chose to talk is their business.
    fine = await client.post(f"/threads/{thread}/messages", json={"body": "hell yes"}, headers=mara)
    assert fine.status_code == 201


async def test_block_removes_their_content_and_tells_moderation(client, make_user, inbox):
    bea, mara, bea_id, mara_id = await _pair(client, make_user)
    someday = await _write(client, bea, "Walk the old Colon street at night")
    thread = (await client.post(f"/threads/solo/{mara_id}", headers=bea)).json()["id"]
    assert someday in await _deck(client, mara)
    # Mara kept it before she thought better of Bea.
    await client.put(f"/somedays/{someday}/keep", json=OPEN, headers=mara)

    assert (await client.put(f"/hidden/{bea_id}", headers=mara)).status_code == 204

    # Gone from the blocker's deck, list, search and threads on the next request.
    assert someday not in await _deck(client, mara)
    assert (await client.get("/me/somedays", headers=mara)).json() == []
    assert (await client.get("/people?q=Bea", headers=mara)).json() == []
    assert (await client.get("/threads", headers=mara)).json() == []
    # And the blocked person cannot reach or see the blocker.
    assert (await client.get("/people?q=Mara", headers=bea)).json() == []
    assert (await client.post(f"/threads/solo/{mara_id}", headers=bea)).status_code == 404
    sent = await client.post(f"/threads/{thread}/messages", json={"body": "hello?"}, headers=bea)
    assert sent.status_code == 404
    assert (await client.post(f"/friends/{mara_id}/request", headers=bea)).status_code == 404

    # The developer is told once, however often the block is repeated.
    assert [alert["subject"] for alert in inbox] == ["blocked: @bea"]
    await client.put(f"/hidden/{bea_id}", headers=mara)
    await client.delete(f"/hidden/{bea_id}", headers=mara)
    # Reversible in Settings: what she kept of Bea's comes back with the unblock.
    kept = (await client.get("/me/somedays", headers=mara)).json()
    assert [row["someday"]["id"] for row in kept] == [someday]
    await client.put(f"/hidden/{bea_id}", headers=mara)
    assert len(inbox) == 1
    async with client.sessionmaker() as session:
        assert [row.reason for row in (await session.scalars(select(Report))).all()] == ["blocked"]


async def test_a_block_holds_for_invites_plans_and_shared_threads(client, make_user, somedays):
    bea, mara, bea_id, mara_id = await _pair(client, make_user)
    # Bea invited Mara, and they share a plan thread Bea has written in.
    await client.put(f"/somedays/{somedays['reef']}/keep", json=OPEN, headers=mara)
    invite = await client.post(
        "/invites",
        json={"to_user_id": mara_id, "someday_id": somedays["reef"], "party": "duo"},
        headers=bea,
    )
    assert invite.status_code == 201, invite.text
    plan = await client.post(
        "/plans",
        json={"someday_id": somedays["lights"], "date": "2030-01-05", "people": [mara_id]},
        headers=bea,
    )
    assert plan.status_code == 201, plan.text
    shared = next(t for t in (await client.get("/threads", headers=bea)).json() if t["kind"] == "plan")
    await client.post(f"/threads/{shared['id']}/messages", json={"body": "see you there"}, headers=bea)

    # Bea blocks Mara: the invite Bea sent is gone from Mara's list too.
    await client.put(f"/hidden/{mara_id}", headers=bea)
    assert (await client.get("/invites", headers=mara)).json() == []
    accepted = await client.post(f"/invites/{invite.json()['id']}/accept", headers=mara)
    assert accepted.status_code == 404

    # The shared thread stays, without the other's words in it or under it.
    listed = next(t for t in (await client.get("/threads", headers=mara)).json() if t["kind"] == "plan")
    assert listed["last_message"] is None
    detail = (await client.get(f"/threads/{shared['id']}", headers=mara)).json()
    assert detail["messages"] == []

    # And a plan is not a way round the block.
    sneaky = await client.post(
        "/plans",
        json={"someday_id": somedays["reef"], "date": "2030-02-01", "people": [bea_id]},
        headers=mara,
    )
    assert sneaky.status_code == 404


async def test_reports_reach_moderation_with_the_content(client, make_user, somedays, inbox):
    bea, mara, bea_id, _ = await _pair(client, make_user)
    someday = await _write(client, bea, "Walk the old Colon street at night")

    flagged = await client.post(
        f"/somedays/{someday}/report", json={"reason": "objectionable"}, headers=mara
    )
    assert flagged.status_code == 204
    reported = await client.post(f"/people/{bea_id}/report", json={"reason": "rude"}, headers=mara)
    assert reported.status_code == 204
    assert "Walk the old Colon street at night" in inbox[0]["text"]
    assert "scripts/moderate.py eject bea" in inbox[1]["text"]
    async with client.sessionmaker() as session:
        reasons = [row.reason for row in (await session.scalars(select(Report))).all()]
    assert reasons == [f"objectionable|someday:{someday}", "rude"]

    # A reason is one of the app's slugs, never free text for a mail subject.
    forged = await client.post(
        f"/people/{bea_id}/report", json={"reason": "rude\nBcc: x@y.z"}, headers=mara
    )
    assert forged.status_code == 422
    # Catalog somedays are not user content, and nobody reports their own.
    for target, who in [(somedays["reef"], mara), (someday, bea)]:
        own = await client.post(f"/somedays/{target}/report", json={"reason": "spam"}, headers=who)
        assert own.status_code == 404


def test_a_moderation_alert_survives_a_line_break_in_its_subject(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(email, "_dispatch", lambda tasks, to, subject, text: sent.append((to, subject, text)))
    email.send_moderation_alert(None, subject="rude:\r\n @bea", text="body")
    assert sent == [("support@gimik.app", "[Gimik moderation] rude: @bea", "body")]


async def test_eject_removes_the_content_and_closes_the_account(client, make_user, somedays):
    bea, mara, bea_id, mara_id = await _pair(client, make_user)
    someday = await _write(client, bea, "Walk the old Colon street at night")
    await client.patch("/auth/me", json={"bio": "hello"}, headers=bea)
    await client.post(
        "/plans",
        json={"someday_id": somedays["lights"], "date": "2030-01-05", "people": [mara_id]},
        headers=bea,
    )

    async with client.sessionmaker() as session:
        assert await moderate.eject(session, "@bea")
        assert not await moderate.eject(session, "nobody")
        assert await session.get(Someday, uuid.UUID(someday)) is None
        gone = await session.get(User, uuid.UUID(bea_id))
        assert (gone.name, gone.bio, gone.city, gone.is_active) == ("Removed account", "", None, False)

    assert someday not in await _deck(client, mara)
    assert (await client.get("/people/bea", headers=mara)).status_code == 404
    assert (await client.get("/plans", headers=mara)).json() == []
    # Nobody can be pointed at the removed account, and its tokens are dead.
    assert (await client.post(f"/threads/solo/{bea_id}", headers=mara)).status_code == 404
    assert (await client.get("/auth/me", headers=bea)).status_code == 401


async def test_a_blocked_member_is_not_listed_in_a_shared_group(client, make_user, make_plus_user):
    bea = await make_plus_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    mara_id = await _id(client, mara)
    group = (await client.post("/groups", json={"name": "Saturday People"}, headers=bea)).json()
    joined = await client.post(f"/groups/join/{group['invite_token']}", headers=mara)
    assert {member["name"] for member in joined.json()["members"]} == {"Bea", "Mara"}

    await client.put(f"/hidden/{mara_id}", headers=bea)

    # Both are still in the group; neither is shown the other in it.
    for headers, seen in [(bea, {"Bea"}), (mara, {"Mara"})]:
        listed = (await client.get(f"/groups/{group['id']}", headers=headers)).json()
        assert {member["name"] for member in listed["members"]} == seen
        thread = next(t for t in (await client.get("/threads", headers=headers)).json() if t["kind"] == "group")
        assert {person["name"] for person in thread["participants"]} == seen
