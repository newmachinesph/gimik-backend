"""Wikidata CC0 importer. Coordinates are copied from Earth P625, never AI."""

import hashlib
import json
import math
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CatalogPlace, Place
from app.models.base import utcnow


def values(entity: dict, property_id: str) -> list:
    return [claim["mainsnak"]["datavalue"]["value"]
            for claim in entity.get("claims", {}).get(property_id, [])
            if claim.get("rank") != "deprecated"
            and "datavalue" in claim.get("mainsnak", {})]


def parse_entity(entity: dict) -> dict:
    entity_id = entity.get("id", "")
    if not re.fullmatch(r"Q[1-9][0-9]*", entity_id):
        raise ValueError("Invalid Wikidata identity")
    if not any(value.get("id") == "Q928" for value in values(entity, "P17")):
        raise ValueError("Source must explicitly identify the Philippines as country")
    coordinates = [point for point in values(entity, "P625")
                   if point.get("globe") == "http://www.wikidata.org/entity/Q2"]
    if len(coordinates) != 1:
        raise ValueError("One unambiguous Earth coordinate is required")
    lat, lon = float(coordinates[0]["latitude"]), float(coordinates[0]["longitude"])
    if not math.isfinite(lat) or not math.isfinite(lon) or not (4 <= lat <= 22 and 116 <= lon <= 127):
        raise ValueError("Coordinates outside the supported Philippines bounds")
    name = entity.get("labels", {}).get("en", {}).get("value", "").strip()
    if not name or len(name) > 160:
        raise ValueError("An English source label is required")
    return {
        "source": "wikidata", "source_id": entity_id,
        "source_url": f"https://www.wikidata.org/wiki/{entity_id}", "license": "CC0-1.0",
        "name": name, "area": "Philippines", "lat": lat, "lon": lon,
        "description": entity.get("descriptions", {}).get("en", {}).get("value", "")[:2000],
        "types": [v["id"] for v in values(entity, "P31") if "id" in v],
        "coordinate_kind": "general_location",
        "source_revision": entity.get("lastrevid"),
    }


async def fetch_entity(entity_id: str) -> dict:
    if not re.fullmatch(r"Q[1-9][0-9]*", entity_id):
        raise ValueError("Expected a Wikidata Q identifier")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.get(
            f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json",
            headers={"User-Agent": "GimikPlaceCatalog/1.0 (https://gimik.app)"},
        )
    response.raise_for_status()
    return parse_entity(response.json()["entities"][entity_id])


async def upsert(session: AsyncSession, evidence: dict, reviewed: bool = False) -> CatalogPlace:
    """Operator-only importer; not exposed as a client-writable API.

    A refreshed source loses approval unless the operator reviews it again.
    Identity is stable, preserving plans and assessment history.
    """
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    row = await session.scalar(select(CatalogPlace).where(
        CatalogPlace.source == "wikidata", CatalogPlace.source_id == evidence["source_id"],
    ))
    if row is None:
        place = Place(provider="wikidata", provider_id=evidence["source_id"],
                      name=evidence["name"], area=evidence["area"],
                      lat=evidence["lat"], lon=evidence["lon"])
        session.add(place)
        await session.flush()
        row = CatalogPlace(place_id=place.id, source="wikidata", source_id=evidence["source_id"])
        session.add(row)
    else:
        place = await session.get(Place, row.place_id)
        place.name, place.area = evidence["name"], evidence["area"]
        place.lat, place.lon = evidence["lat"], evidence["lon"]
    row.reviewed = reviewed or (row.reviewed and row.evidence_hash == fingerprint)
    row.source_url, row.license = evidence["source_url"], evidence["license"]
    row.evidence, row.evidence_hash = evidence, fingerprint
    row.coordinate_kind, row.updated_at = "general_location", utcnow()
    await session.flush()
    return row
