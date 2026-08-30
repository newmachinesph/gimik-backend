"""Place search: provider hit once, cached forever, and plans carry the pick."""

from __future__ import annotations

from app.routers import places as places_router

FAKE_ROWS = [
    {
        "place_id": "324251175203",
        "display_place": "Toyo Eatery",
        "display_name": "Toyo Eatery, Chino Roces Avenue Extension, Makati",
        "display_address": "Chino Roces Avenue Extension, Makati",
        "lat": "14.5312541",
        "lon": "121.0226562",
        "address": {"road": "Chino Roces Avenue Extension", "city": "Makati"},
    }
]


async def test_search_caches_provider_results(client, make_user, monkeypatch):
    me = await make_user("Bea")
    calls = {"n": 0}

    async def fake_provider(q: str):
        calls["n"] += 1
        return FAKE_ROWS

    monkeypatch.setattr(places_router, "_provider_search", fake_provider)

    first = await client.get("/places/search", params={"q": "toyo eatery"}, headers=me)
    assert first.status_code == 200
    assert [row["name"] for row in first.json()] == ["Toyo Eatery"]
    assert first.json()[0]["area"] == "Chino Roces Avenue Extension, Makati"
    assert calls["n"] == 1

    # The second search is answered from the places table - no provider call.
    again = await client.get("/places/search", params={"q": "toyo"}, headers=me)
    assert again.status_code == 200
    assert [row["name"] for row in again.json()] == ["Toyo Eatery"]
    assert calls["n"] == 1


async def test_plan_carries_the_place(client, somedays, make_user, monkeypatch):
    me = await make_user("Bea")

    async def fake_provider(q: str):
        return FAKE_ROWS

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


async def test_search_requires_three_characters(client, make_user):
    me = await make_user("Bea")
    short = await client.get("/places/search", params={"q": "to"}, headers=me)
    assert short.status_code == 422
