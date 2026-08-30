"""Place search: own table first, LocationIQ second, and every provider answer
cached into the table - so a place anyone has picked never costs a request
again, and the rate limit is spent only on genuinely new names.

The app never talks to LocationIQ directly in production: the key lives here,
server-side, and this endpoint returns the same shape the app's demo-mode
searcher produces.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import current_user
from app.models import Place, User
from app.schemas import PlaceOut

router = APIRouter(prefix="/places", tags=["places"])
settings = get_settings()

PROVIDER_URL = "https://api.locationiq.com/v1/autocomplete"


def _area_of(address: dict[str, str], display_address: str) -> str:
    bits = [
        address.get("road"),
        address.get("neighbourhood") or address.get("suburb"),
        address.get("city") or address.get("state"),
    ]
    area = ", ".join([bit for bit in bits if bit][:2])
    return area or ", ".join(display_address.split(",")[:2]).strip()


async def _provider_search(q: str) -> list[dict]:
    """LocationIQ autocomplete, PH only. An empty list is the whole failure
    mode: no key, rate limited, or nothing found all look the same to the app,
    which falls back to free-text places."""
    if not settings.locationiq_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                PROVIDER_URL,
                params={
                    "key": settings.locationiq_key,
                    "q": q,
                    "countrycodes": "ph",
                    "limit": 6,
                    "dedupe": 1,
                },
            )
        if response.status_code != 200:
            return []
        return response.json()
    except httpx.HTTPError:
        return []


@router.get("/search", response_model=list[PlaceOut])
async def search(
    q: str = Query(min_length=3, max_length=120),
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PlaceOut]:
    needle = q.strip()

    cached = (
        await session.scalars(
            select(Place).where(Place.name.ilike(f"%{needle}%")).order_by(Place.name).limit(6)
        )
    ).all()
    if cached:
        return [PlaceOut.model_validate(row) for row in cached]

    rows = await _provider_search(needle)
    out: list[Place] = []
    for row in rows:
        provider_id = str(row.get("place_id", ""))
        if not provider_id:
            continue
        existing = await session.scalar(
            select(Place).where(Place.provider == "locationiq", Place.provider_id == provider_id)
        )
        if existing is not None:
            out.append(existing)
            continue
        name = row.get("display_place") or str(row.get("display_name", "")).split(",")[0]
        place = Place(
            provider="locationiq",
            provider_id=provider_id,
            name=name[:160],
            area=_area_of(row.get("address") or {}, row.get("display_address") or row.get("display_name", ""))[:200],
            lat=float(row.get("lat", 0)),
            lon=float(row.get("lon", 0)),
        )
        session.add(place)
        out.append(place)
    await session.commit()
    return [PlaceOut.model_validate(row) for row in out]
