"""Place search: Plus-gated, provider hit once, cached, and plans carry the pick."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Place
from app.routers import places as places_router

# One Google Places Autocomplete suggestion, in the shape the field mask asks
# for. No coordinates: Autocomplete does not return them, which is the whole
# reason the columns are nullable.
FAKE_SUGGESTIONS = [
    {
        "placePrediction": {
            "placeId": "ChIJd8sJ5PPJlzMRm1LzYqVjKZI",
            "structuredFormat": {
                "mainText": {"text": "Toyo Eatery"},
                "secondaryText": {"text": "Chino Roces Avenue Extension, Makati"},
            },
        }
    }
]


async def test_search_caches_provider_results(client, make_plus_user, monkeypatch):
    me = await make_plus_user("Bea")
    calls = {"n": 0}

    async def fake_provider(q: str, session_token: str | None):
        calls["n"] += 1
        return FAKE_SUGGESTIONS

    monkeypatch.setattr(places_router, "_provider_search", fake_provider)

    first = await client.get("/places/search", params={"q": "toyo eatery"}, headers=me)
    assert first.status_code == 200
    assert [row["name"] for row in first.json()] == ["Toyo Eatery"]
    assert first.json()[0]["area"] == "Chino Roces Avenue Extension, Makati"
    assert first.json()[0]["lat"] is None
    assert calls["n"] == 1

    # The second search is answered from the places table - no provider call.
    again = await client.get("/places/search", params={"q": "toyo"}, headers=me)
    assert again.status_code == 200
    assert [row["name"] for row in again.json()] == ["Toyo Eatery"]
    assert calls["n"] == 1


async def test_search_is_plus_only(client, make_user, make_plus_user, monkeypatch):
    """The dearest call in the stack, and the one a free account does without.

    402 rather than 403: the app opens its paywall on this status, and a free
    account can still type the place name by hand.
    """
    called = {"n": 0}

    async def fake_provider(q: str, session_token: str | None):
        called["n"] += 1
        return FAKE_SUGGESTIONS

    monkeypatch.setattr(places_router, "_provider_search", fake_provider)

    free = await make_user("Bea")
    refused = await client.get("/places/search", params={"q": "toyo eatery"}, headers=free)
    assert refused.status_code == 402
    # The point of the gate: the refusal costs nothing at Google.
    assert called["n"] == 0

    paid = await make_plus_user("Cy")
    allowed = await client.get("/places/search", params={"q": "toyo eatery"}, headers=paid)
    assert allowed.status_code == 200
    assert called["n"] == 1


async def test_stale_rows_are_refreshed_not_reserved(client, make_plus_user, monkeypatch):
    """Google's terms cap how long the display text may be served.

    A row past the window is refreshed in place - same row id, so every plan
    already pointing at it survives - rather than served or duplicated.
    """
    me = await make_plus_user("Bea")

    async def fake_provider(q: str, session_token: str | None):
        return FAKE_SUGGESTIONS

    monkeypatch.setattr(places_router, "_provider_search", fake_provider)
    first = await client.get("/places/search", params={"q": "toyo eatery"}, headers=me)
    original_id = first.json()[0]["id"]

    # Age the row past the cache window.
    async with client.sessionmaker() as session:
        row = await session.scalar(select(Place))
        assert row is not None
        row.refreshed_at = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=places_router.PLACE_CACHE_DAYS + 1
        )
        await session.commit()

    again = await client.get("/places/search", params={"q": "toyo eatery"}, headers=me)
    assert again.status_code == 200
    # Same row, renewed - not a second copy of the same place.
    assert again.json()[0]["id"] == original_id
    async with client.sessionmaker() as session:
        assert len((await session.scalars(select(Place))).all()) == 1


async def test_plan_carries_the_place(client, somedays, make_plus_user, monkeypatch):
    me = await make_plus_user("Bea")

    async def fake_provider(q: str, session_token: str | None):
        return FAKE_SUGGESTIONS

    monkeypatch.setattr(places_router, "_provider_search", fake_provider)
    place = (await client.get("/places/search", params={"q": "toyo eatery"}, headers=me)).json()[0]

    locked = await client.post(
        "/plans",
        json={
            "someday_id": somedays["reef"],
            "date": "2026-09-05",
            "place": f"{place['name']} · {place['area']}",
            "place_id": place["id"],
        },
        headers=me,
    )
    assert locked.status_code == 201
    assert locked.json()["place"].startswith("Toyo Eatery")
    assert locked.json()["place_id"] == place["id"]


async def test_search_requires_three_characters(client, make_plus_user):
    me = await make_plus_user("Bea")
    short = await client.get("/places/search", params={"q": "to"}, headers=me)
    assert short.status_code == 422
