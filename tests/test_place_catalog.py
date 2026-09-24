"""No live provider calls: all AI/provider boundaries are faked."""

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import CatalogPlace, Place, PlaceAssessment, PlaceSearchEvent
from app.models.base import utcnow
from app.routers import places
from app.services import place_catalog as service
from app.services.wikidata_places import parse_entity, upsert


async def seed(client, *, reviewed=True, description="A public reef for diving"):
    evidence = {
        "source_id": "Q123", "source_url": "https://www.wikidata.org/wiki/Q123",
        "license": "CC0-1.0", "name": "Test Reef", "area": "Cebu, Philippines",
        "lat": 10.2, "lon": 123.8, "description": description,
    }
    async with client.sessionmaker() as session:
        row = await upsert(session, evidence, reviewed=reviewed)
        await session.commit()
        return row.id


async def test_free_catalogue_hit_no_google_and_every_search_recorded(client, make_user, monkeypatch):
    me = await make_user("Bea")
    await seed(client)

    async def forbidden(*args):
        pytest.fail("Catalogue hit must not call Google")

    monkeypatch.setattr(places, "_provider_search", forbidden)
    for _ in range(2):
        result = await client.get("/places/search", params={"q": "reef"}, headers=me)
        assert result.status_code == 200
        assert result.json()[0]["lat"] == 10.2
        assert result.json()[0]["provider"] == "wikidata"
    async with client.sessionmaker() as session:
        events = (await session.scalars(select(PlaceSearchEvent))).all()
        assert len(events) == 2
        assert all(e.outcome == "catalogue" and e.query == "reef" for e in events)


async def test_unknown_place_not_published_and_misses_logged(client, make_user):
    me = await make_user("Bea")
    await seed(client, reviewed=False)
    response = await client.get("/places/search", params={"q": "reef", "live": False}, headers=me)
    assert response.json() == []
    async with client.sessionmaker() as session:
        event = await session.scalar(select(PlaceSearchEvent))
        assert event.outcome == "no_match"


async def test_activity_pairs_queued_once_then_reused(client, make_user, somedays, monkeypatch):
    me = await make_user("Bea")
    await seed(client)
    params = {"q": "reef", "activity": somedays["reef"], "live": False}
    for _ in range(2):
        assert (await client.get("/places/search", params=params, headers=me)).json() == []
    called = []

    async def fake_assess(evidence, activity):
        called.append((evidence, activity))
        assert "user_id" not in evidence and "query" not in evidence
        return service.AssessmentResult(verdict="suitable", confidence=0.95, reason="Explicit diving reef")

    monkeypatch.setattr(get_settings(), "openai_api_key", "test")
    monkeypatch.setattr(service, "assess", fake_assess)
    async with client.sessionmaker() as session:
        assert len((await session.scalars(select(PlaceAssessment))).all()) == 1
        assert await service.drain(session) == 1
        assert await service.drain(session) == 0
    result = await client.get("/places/search", params=params, headers=me)
    assert result.json()[0]["name"] == "Test Reef"
    assert len(called) == 1
    # The same place is not automatically suitable for a different activity.
    params["activity"] = somedays["lights"]
    assert (await client.get("/places/search", params=params, headers=me)).json() == []


@pytest.mark.parametrize("verdict,confidence", [("uncertain", 0.99), ("unsuitable", 1), ("suitable", 0.7)])
async def test_uncertain_or_low_confidence_not_suggested(client, make_user, somedays, monkeypatch, verdict, confidence):
    me = await make_user("Bea")
    await seed(client)
    params = {"activity": somedays["reef"], "live": False}
    await client.get("/places/search", params=params, headers=me)

    async def fake_assess(*args):
        return service.AssessmentResult(verdict=verdict, confidence=confidence, reason="Not enough evidence")

    monkeypatch.setattr(get_settings(), "openai_api_key", "test")
    monkeypatch.setattr(service, "assess", fake_assess)
    async with client.sessionmaker() as session:
        await service.drain(session)
    assert (await client.get("/places/search", params=params, headers=me)).json() == []


async def test_provider_error_and_paywall_are_recorded(client, make_user, make_plus_user, monkeypatch):
    free = await make_user("Bea")
    paid = await make_plus_user("Cy")
    assert (await client.get("/places/search", params={"q": "reef"}, headers=free)).status_code == 402

    async def broken(*args):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(places, "_provider_search", broken)
    assert (await client.get("/places/search", params={"q": "reef"}, headers=paid)).json() == []
    async with client.sessionmaker() as session:
        outcomes = (await session.scalars(select(PlaceSearchEvent.outcome))).all()
        assert outcomes == ["plus_required", "provider_error"]


async def test_history_delete_is_private_and_retention_expires(client, make_user):
    first, second = await make_user("Bea"), await make_user("Cy")
    for me in [first, second]:
        await client.get("/places/search", params={"q": "reef", "live": False}, headers=me)
    assert (await client.delete("/places/search-history", headers=first)).status_code == 204
    async with client.sessionmaker() as session:
        events = (await session.scalars(select(PlaceSearchEvent))).all()
        assert len(events) == 1
        events[0].created_at = utcnow() - dt.timedelta(days=91)
        session.add(Place(provider="google", provider_id="old", name="Legacy text", area="Old",
                          lat=10, lon=120))
        await session.commit()
        assert await service.purge_search_history(session) == 1
        row = await session.scalar(select(Place).where(Place.provider == "google"))
        await session.refresh(row)
        assert row.name == "" and row.lat is None


async def test_changed_evidence_requires_review_and_new_assessment(client, make_user, somedays):
    me = await make_user("Bea")
    cid = await seed(client)
    params = {"activity": somedays["reef"], "live": False}
    await client.get("/places/search", params=params, headers=me)
    await seed(client, reviewed=False, description="Source changed")
    async with client.sessionmaker() as session:
        row = await session.get(CatalogPlace, cid)
        assert not row.reviewed
    assert (await client.get("/places/search", params=params, headers=me)).json() == []


async def test_ai_failure_retries_bounded_and_missing_key_inert(client, make_user, somedays, monkeypatch):
    me = await make_user("Bea")
    await seed(client)
    await client.get("/places/search", params={"activity": somedays["reef"], "live": False}, headers=me)
    async with client.sessionmaker() as session:
        assert await service.drain(session) == 0
    monkeypatch.setattr(get_settings(), "openai_api_key", "test")

    async def broken(*args):
        raise ValueError("Invalid/refused output")

    monkeypatch.setattr(service, "assess", broken)
    async with client.sessionmaker() as session:
        for attempt in range(1, 4):
            assert await service.drain(session) == 1
            job = await session.scalar(select(PlaceAssessment))
            assert job.attempts == attempt
            job.available_at = utcnow() - dt.timedelta(seconds=1)
            await session.commit()
        assert job.status == "failed"
        assert await service.drain(session) == 0


async def test_private_or_nonexistent_activity_rejected(client, make_user):
    me = await make_user("Bea")
    response = await client.get("/places/search", params={"activity": str(uuid.uuid4())}, headers=me)
    assert response.status_code == 404
    assert (await client.get("/places/search", params={"q": "   "}, headers=me)).status_code == 422
    assert (await client.get("/places/search", params={"q": "reef"})).status_code == 401


def entity():
    return {"id": "Q123", "labels": {"en": {"value": "Test Reef"}}, "claims": {
        "P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q928"}}}}],
        "P625": [{"mainsnak": {"datavalue": {"value": {
            "latitude": 10.2, "longitude": 123.8, "globe": "http://www.wikidata.org/entity/Q2",
        }}}}],
    }}


def test_import_requires_real_source_coordinates_and_country():
    original = entity()
    result = parse_entity(original)
    assert result["coordinate_kind"] == "general_location"
    assert result["lat"] == 10.2
    original["claims"]["P17"] = []
    with pytest.raises(ValueError, match="Philippines"):
        parse_entity(original)


@pytest.mark.parametrize("latitude", [float("nan"), 45, -10])
def test_bad_coordinates_rejected(latitude):
    original = entity()
    original["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]["latitude"] = latitude
    with pytest.raises(ValueError, match="bounds"):
        parse_entity(original)


async def test_free_plan_cannot_use_google_identity(client, make_user, somedays):
    me = await make_user("Bea")
    async with client.sessionmaker() as session:
        row = Place(provider="google", provider_id="google-only", name="", area="")
        session.add(row)
        await session.commit()
        pid = str(row.id)
    response = await client.post("/plans", headers=me, json={
        "someday_id": somedays["reef"], "date": "2026-10-01", "place_id": pid,
    })
    assert response.status_code == 402


async def test_sql_wildcards_are_literal(client, make_user):
    me = await make_user("Bea")
    await seed(client)
    response = await client.get("/places/search", headers=me, params={"q": "%%%", "live": False})
    assert response.json() == []


async def test_ai_wire_format_and_refusal(monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_api_key", "test")
    captured = []

    def handler(request):
        import json

        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "Cannot assess"}]},
        ]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    with pytest.raises(ValueError):
        await service.assess({"description": "public park"}, {"title": "picnic"})
    assert captured[0]["store"] is False
    assert captured[0]["text"]["format"]["strict"] is True
    assert "lat" not in captured[0]["text"]["format"]["schema"]["properties"]
