"""Open-data suggestions first; paid Google autocomplete on misses.

Only Google place IDs persist, never provider result text or AI copies of it.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import current_user
from app.entitlements import PlusRequired
from app.models import Place, PlaceSearchEvent, Someday, User
from app.schemas import PlaceOut
from app.services.place_catalog import suggestions
from app.services.ratelimit import Limit, limiter

router = APIRouter(prefix="/places", tags=["places"])
settings = get_settings()
log = logging.getLogger("gimik.places")
PROVIDER = "google"
PROVIDER_URL = "https://places.googleapis.com/v1/places:autocomplete"
RESULT_LIMIT = 6
FIELD_MASK = (
    "suggestions.placePrediction.placeId,"
    "suggestions.placePrediction.structuredFormat.mainText.text,"
    "suggestions.placePrediction.structuredFormat.secondaryText.text"
)


async def _provider_search(q: str, session_token: str | None) -> list[dict]:
    if not settings.google_maps_api_key:
        return []
    body: dict = {"input": q, "includedRegionCodes": ["ph"], "languageCode": "en"}
    if session_token:
        body["sessionToken"] = session_token
    async with httpx.AsyncClient(timeout=6.0) as client:
        response = await client.post(PROVIDER_URL, json=body, headers={
            "X-Goog-Api-Key": settings.google_maps_api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        })
    response.raise_for_status()
    return response.json().get("suggestions", [])


async def ensure_coordinates(session: AsyncSession, place: Place) -> Place:
    """Resolve a selected Google place only; no commit inside a plan transaction."""
    if place.provider != PROVIDER or not settings.google_maps_api_key:
        return place
    now = dt.datetime.now(dt.UTC)
    stamp = place.coordinates_refreshed_at
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    if (stamp and stamp > now - dt.timedelta(days=29)
            and place.lat is not None and place.lon is not None):
        return place
    place.lat = place.lon = None
    place.coordinates_refreshed_at = None
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(
                f"https://places.googleapis.com/v1/places/{place.provider_id}",
                headers={"X-Goog-Api-Key": settings.google_maps_api_key,
                         "X-Goog-FieldMask": "location"},
            )
        response.raise_for_status()
        point = response.json()["location"]
        lat, lon = float(point["latitude"]), float(point["longitude"])
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            place.lat, place.lon, place.coordinates_refreshed_at = lat, lon, now
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        log.warning("place coordinates unavailable")
    return place


def _prediction(suggestion: dict) -> tuple[str, str, str] | None:
    prediction = suggestion.get("placePrediction") or {}
    place_id = prediction.get("placeId")
    structured = prediction.get("structuredFormat") or {}
    name = (structured.get("mainText") or {}).get("text") or ""
    area = (structured.get("secondaryText") or {}).get("text") or ""
    if not place_id or not name:
        return None
    return str(place_id), name[:160], area[:200]


async def _activity(session: AsyncSession, key: str | None, user: User) -> Someday | None:
    if not key:
        return None
    try:
        activity = await session.get(Someday, uuid.UUID(key))
    except ValueError:
        activity = await session.scalar(select(Someday).where(Someday.slug == key))
    if activity is None or (activity.owner_id is not None and activity.owner_id != user.id):
        raise HTTPException(404, "No such activity")
    return activity


async def _google_identity(session: AsyncSession, provider_id: str) -> Place:
    statement = select(Place).where(Place.provider == PROVIDER, Place.provider_id == provider_id)
    existing = await session.scalar(statement)
    if existing is not None:
        return existing
    try:
        async with session.begin_nested():
            row = Place(provider=PROVIDER, provider_id=provider_id, name="", area="")
            session.add(row)
            await session.flush()
        return row
    except IntegrityError:
        row = await session.scalar(statement)
        assert row is not None
        return row


@router.get("/search", response_model=list[PlaceOut], dependencies=[
    Depends(limiter(Limit(times=60, seconds=60, bucket="places"))),
])
async def search(
    q: str = Query(default="", max_length=120),
    activity: str | None = Query(default=None, max_length=120),
    live: bool = True,
    session_token: str | None = Query(default=None, max_length=64, alias="session"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PlaceOut]:
    needle = " ".join(q.split())
    if len(needle) < 3 and (needle or not activity):
        raise HTTPException(422, "Enter at least three characters or choose an activity")
    chosen_activity = await _activity(session, activity, user)
    event = PlaceSearchEvent(user_id=user.id, query=needle,
                             someday_id=chosen_activity.id if chosen_activity else None)
    session.add(event)
    rows = await suggestions(session, needle, chosen_activity, RESULT_LIMIT)
    if rows or not live or not needle:
        event.outcome, event.result_count = "catalogue" if rows else "no_match", len(rows)
        await session.commit()
        return [PlaceOut.model_validate(row) for row in rows]
    if not user.is_plus:
        event.outcome = "plus_required"
        await session.commit()
        raise PlusRequired("Place search")
    event.outcome = "provider_started"
    await session.commit()
    try:
        predictions = await _provider_search(needle, session_token)
    except (httpx.HTTPError, ValueError, TypeError):
        event.outcome = "provider_error"
        await session.commit()
        log.warning("place provider request failed")
        return []
    out = []
    seen = set()
    for prediction in predictions[:RESULT_LIMIT]:
        parsed = _prediction(prediction)
        if parsed is None:
            continue
        provider_id, name, area = parsed
        if provider_id in seen:
            continue
        seen.add(provider_id)
        row = await _google_identity(session, provider_id)
        out.append(PlaceOut(id=row.id, name=name, area=area, provider="google"))
    event.outcome, event.result_count = "google" if out else "no_match", len(out)
    await session.commit()
    return out


@router.delete("/search-history", status_code=204)
async def clear_search_history(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
) -> None:
    await session.execute(delete(PlaceSearchEvent).where(PlaceSearchEvent.user_id == user.id))
    await session.commit()
