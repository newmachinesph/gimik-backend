"""Seed the catalog from seed/catalog.json.

The JSON is the same 522-concept catalog the app bundles (exported from
`gora-v2/src/lib/catalog.ts`), and every slug's artwork already exists in the
v1 MinIO as `gora-catalog/catalog/<slug>.webp`. Seeding is therefore rows only:
no images move, the store already has them.

Idempotent: rows are upserted by slug, so running it twice changes nothing.

    python scripts/seed.py [--verify-artwork]

--verify-artwork HEADs each slug's object in the catalog bucket and reports the
missing ones instead of trusting that the v1 sync ran.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Someday

CATALOG_PATH = Path(__file__).resolve().parent.parent / "seed" / "catalog.json"


async def seed() -> tuple[int, int]:
    entries = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    created = updated = 0
    async with SessionLocal() as session:
        existing = {
            someday.slug: someday
            for someday in (
                await session.scalars(select(Someday).where(Someday.slug.is_not(None)))
            ).all()
        }
        for entry in entries:
            row = existing.get(entry["slug"])
            if row is None:
                session.add(
                    Someday(
                        slug=entry["slug"],
                        title=entry["title"],
                        category=entry["category"],
                        domain=entry.get("domain"),
                        register=entry.get("register"),
                        party=entry.get("party", "any"),
                        location=entry.get("location"),
                        needs=entry.get("needs", []),
                    )
                )
                created += 1
            else:
                row.title = entry["title"]
                row.category = entry["category"]
                row.domain = entry.get("domain")
                row.register = entry.get("register")
                row.party = entry.get("party", "any")
                row.location = entry.get("location")
                row.needs = entry.get("needs", [])
                updated += 1
        await session.commit()
    return created, updated


def verify_artwork() -> list[str]:
    from botocore.exceptions import ClientError

    from app.config import get_settings
    from app.services.storage import _client

    settings = get_settings()
    entries = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    client = _client()
    missing: list[str] = []
    for entry in entries:
        key = f"catalog/{entry['slug']}.webp"
        try:
            client.head_object(Bucket=settings.s3_catalog_bucket, Key=key)
        except ClientError:
            missing.append(entry["slug"])
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-artwork", action="store_true")
    args = parser.parse_args()

    created, updated = asyncio.run(seed())
    print(f"catalog: {created} created, {updated} updated")

    if args.verify_artwork:
        missing = verify_artwork()
        if missing:
            print(f"artwork MISSING for {len(missing)} slugs:")
            for slug in missing:
                print(f"  {slug}")
            sys.exit(1)
        print("artwork: every slug has its object in the catalog bucket")


if __name__ == "__main__":
    main()
