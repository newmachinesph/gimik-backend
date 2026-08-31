"""Place search: own table first, Google Places Autocomplete second, and every
provider answer cached into the table - so a place anyone has picked never
costs a request again, and the quota is spent only on genuinely new names.

The app never talks to Google directly: the key lives here, server-side, and
this endpoint returns the same shape the app's demo-mode searcher produces.

**Autocomplete only, never Place Details.** A prediction carries a place ID and
two lines of text, which is exactly what the product shows - a plan records the
place as the words someone would say out loud, not a coordinate. Resolving
coordinates would mean a Details call per result, six times the cost, for a
number nothing reads. If a map ever arrives, resolve then, once, on the place
actually chosen.

**Caching is bounded by Google's terms.** Place IDs may be kept indefinitely;
the display text that comes with them may not. A row older than
PLACE_CACHE_DAYS is therefore refreshed from the provider rather than served,
while the ID it is keyed by survives - which keeps every plan already pointing
at that row intact.

Search is a Gora Plus feature. It is the dearest call in the stack by a wide
margin, and free accounts type the place name instead, which the plan screen
has always accepted.
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.entitlements import requires_plus
from app.models import Place, User
from app.schemas import PlaceOut

router = APIRouter(prefix="/places", tags=["places"])
settings = get_settings()
log = logging.getLogger("gora.places")

PROVIDER = "google"
PROVIDER_URL = "https://places.googleapis.com/v1/places:autocomplete"
# Google's terms allow place IDs to be stored indefinitely and other content
# for a limited period. Thirty days is the conservative reading.
PLACE_CACHE_DAYS = 30
RESULT_LIMIT = 6
# Only what is displayed. A narrower mask is a cheaper call as well as a
# smaller response.
FIELD_MASK = (
    "suggestions.placePrediction.placeId,"
    "suggestions.placePrediction.structuredFormat.mainText.text,"
    "suggestions.placePrediction.structuredFormat.secondaryText.text"
)


def _fresh_cutoff() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=PLACE_CACHE_DAYS)


def _is_fresh(place: Place) -> bool:
    stamp = place.refreshed_at
    if stamp is None:
        return False
    # SQLite hands back naive datetimes; Postgres does not.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return stamp > _fresh_cutoff()


async def _provider_search(q: str, session_token: str | None) -> list[dict]:
    """Google Places Autocomplete, PH only.

    An empty list is the whole failure mode: no key, quota spent, or nothing
    found all look the same to the app, which falls back to free-text places.

    The session token matters for the bill, not the result. Google groups the
    keystrokes of one search into a single billable session when they share a
    token; without one, every debounced request is priced on its own.
    """
    if not settings.google_maps_api_key:
        return []
    body: dict[str, object] = {
        "input": q,
        "includedRegionCodes": ["ph"],
        "languageCode": "en",
    }
    if session_token:
        body["sessionToken"] = session_token
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.post(
                PROVIDER_URL,
                json=body,
                headers={
                    "X-Goog-Api-Key": settings.google_maps_api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                    "Content-Type": "application/json",
                },
            )
        if response.status_code != 200:
            # Logged rather than swallowed: a spent quota and a bad key look
            # identical from the app, and only this line tells them apart.
            log.warning("places autocomplete %s: %s", response.status_code, response.text[:200])
            return []
        return response.json().get("suggestions", [])
    except httpx.HTTPError:
        log.exception("places autocomplete failed")
        return []


def _prediction(suggestion: dict) -> tuple[str, str, str] | None:
    """(place_id, name, area) from one suggestion, or None if it is unusable."""
    prediction = suggestion.get("placePrediction") or {}
    place_id = prediction.get("placeId")
    if not place_id:
        return None
    structured = prediction.get("structuredFormat") or {}
    name = (structured.get("mainText") or {}).get("text") or ""
    area = (structured.get("secondaryText") or {}).get("text") or ""
    if not name:
        return None
    return str(place_id), name[:160], area[:200]


@router.get("/search", response_model=list[PlaceOut])
async def search(
    q: str = Query(min_length=3, max_length=120),
    session_token: str | None = Query(
        default=None,
        max_length=64,
        alias="session",
        description="Groups one search's keystrokes into a single billable session.",
    ),
    _user: User = Depends(requires_plus("Place search")),
    session: AsyncSession = Depends(get_session),
) -> list[PlaceOut]:
    needle = q.strip()

    # Own table first. This is the whole cost story: a place someone has
    # already picked is answered from Postgres and never reaches Google.
    cached = (
        await session.scalars(
            select(Place)
            .where(Place.name.ilike(f"%{needle}%"), Place.refreshed_at > _fresh_cutoff())
            .order_by(Place.name)
            .limit(RESULT_LIMIT)
        )
    ).all()
    if cached:
        return [PlaceOut.model_validate(row) for row in cached]

    now = dt.datetime.now(dt.UTC)
    out: list[Place] = []
    for suggestion in await _provider_search(needle, session_token):
        parsed = _prediction(suggestion)
        if parsed is None:
            continue
        place_id, name, area = parsed
        existing = await session.scalar(
            select(Place).where(Place.provider == PROVIDER, Place.provider_id == place_id)
        )
        if existing is not None:
            # Refresh in place rather than inserting: the row's id is already
            # referenced by plans, and the terms ask for the text to be renewed
            # rather than retained.
            existing.name = name
            existing.area = area
            existing.refreshed_at = now
            out.append(existing)
            continue
        place = Place(
            provider=PROVIDER,
            provider_id=place_id,
            name=name,
            area=area,
            refreshed_at=now,
        )
        session.add(place)
        out.append(place)
    await session.commit()
    return [PlaceOut.model_validate(row) for row in out]
