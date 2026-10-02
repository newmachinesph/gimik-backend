"""App Store guideline 1.2: filter, flag, block, and act on it.

Each test sends the request the app would send. The moderation inbox is a list
here, so "the developer was told" is something a test can read.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Report, Someday
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
    return {item["someday"]["id"] for item in (await client.get("/feed", headers=headers)).json()["items"]}


def test_filter_knows_abuse_from_ordinary_words():
    assert moderation.objectionable("you are a f4ggot")
    assert moderation.objectionable("what the fuuuuck")
    assert moderation.objectionable("Putang ina mo")
    # Messages are held only to the severe list.
    assert not moderation.objectionable("fuck yes, Saturday", public=False)
    assert moderation.objectionable("kys", public=False)
    # Words that merely contain or resemble a blocked one.
    for clean in ["Scunthorpe cocktail class", "magna cum laude", "paki-abot", "shiitake", ""]:
        assert not moderation.objectionable(clean), clean


async def test_objectionable_text_is_refused_where_strangers_read_it(client, make_user):
    bea = await make_user("Bea", city="Cebu City")
    refused = await client.post(
        "/somedays", json={"title": "Fuck around downtown", "category": "Food & Drink", **OPEN}, headers=bea
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == moderation.REJECTED
    assert (await client.patch("/auth/me", json={"bio": "total slut"}, headers=bea)).status_code == 422
    named = await client.post(
        "/auth/register",
        json={"email": "x@test.dev", "password": "long-enough-pass", "name": "Nigger"},
    )
    assert named.status_code == 422
    await _write(client, bea, "Find the bakery lola talks about")


async def test_block_removes_their_content_and_tells_moderation(client, make_user, inbox):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    bea_id, mara_id = await _id(client, bea), await _id(client, mara)
    someday = await _write(client, bea, "Walk the old Colon street at night")
    thread = (await client.post(f"/threads/solo/{mara_id}", headers=bea)).json()["id"]
    assert someday in await _deck(client, mara)

    assert (await client.put(f"/hidden/{bea_id}", headers=mara)).status_code == 204

    # Gone from the blocker's deck, search and threads on the very next request.
    assert someday not in await _deck(client, mara)
    assert (await client.get("/people?q=Bea", headers=mara)).json() == []
    assert (await client.get("/threads", headers=mara)).json() == []
    # And the blocked person cannot reach or see the blocker.
    assert (await client.get("/people?q=Mara", headers=bea)).json() == []
    assert (await client.post(f"/threads/solo/{mara_id}", headers=bea)).status_code == 404
    sent = await client.post(f"/threads/{thread}/messages", json={"body": "hello?"}, headers=bea)
    assert sent.status_code == 404
    assert (await client.post(f"/friends/{mara_id}/request", headers=bea)).status_code == 404

    # The developer is told, once, and the block is on file as a report.
    assert [alert["subject"] for alert in inbox] == ["blocked: @bea"]
    await client.put(f"/hidden/{bea_id}", headers=mara)
    assert len(inbox) == 1
    async with client.sessionmaker() as session:
        assert [row.reason for row in (await session.scalars(select(Report))).all()] == ["blocked"]

    # Reversible in Settings.
    await client.delete(f"/hidden/{bea_id}", headers=mara)
    assert someday in await _deck(client, mara)


async def test_reports_reach_moderation_with_the_content(client, make_user, somedays, inbox):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    bea_id = await _id(client, bea)
    someday = await _write(client, bea, "Walk the old Colon street at night")

    flagged = await client.post(f"/somedays/{someday}/report", json={"reason": "objectionable"}, headers=mara)
    assert flagged.status_code == 204
    assert (await client.post(f"/people/{bea_id}/report", json={"reason": "rude"}, headers=mara)).status_code == 204
    assert "Walk the old Colon street at night" in inbox[0]["text"]
    assert "scripts/moderate.py eject bea" in inbox[1]["text"]
    async with client.sessionmaker() as session:
        reasons = [row.reason for row in (await session.scalars(select(Report))).all()]
    assert reasons == [f"objectionable|someday:{someday}", "rude"]

    # Catalog somedays are not user content, and nobody reports their own.
    for target, who in [(somedays["reef"], mara), (someday, bea)]:
        assert (await client.post(f"/somedays/{target}/report", json={"reason": "spam"}, headers=who)).status_code == 404


async def test_eject_removes_the_content_and_closes_the_account(client, make_user):
    bea = await make_user("Bea", city="Cebu City")
    mara = await make_user("Mara", city="Cebu City")
    someday = await _write(client, bea, "Walk the old Colon street at night")
    await client.patch("/auth/me", json={"bio": "hello"}, headers=bea)

    async with client.sessionmaker() as session:
        assert await moderate.eject(session, "@bea")
        assert not await moderate.eject(session, "nobody")
        assert await session.get(Someday, uuid.UUID(someday)) is None

    assert someday not in await _deck(client, mara)
    assert (await client.get("/people/bea", headers=mara)).status_code == 404
    # Every token already issued stops working, and the address stays taken.
    assert (await client.get("/auth/me", headers=bea)).status_code == 401
