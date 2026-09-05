"""Seed the activity catalog: 2,000 concepts and their artwork, from one export.

The export is a zip holding a SQLite catalog (`activities.db`) and one WebP per
activity (`images_webp/<id>-<slug>.webp`). The database is the source of truth
for the words - name, category, subcategory - and the zip is the source of truth
for the picture. Nothing here invents either.

Two things it has to reconcile. The database records `image_file` with a `.png`
extension from an earlier render pass, while the zip actually ships `.webp`; the
slug is the join key, not the recorded filename. And the API serves artwork from
`catalog/<slug>.webp` in the public catalog bucket, so that - not the export's
numbered name - is the object key.

Idempotent by construction: rows upsert on slug, and an object already in the
bucket at the right size is left alone. Re-running after a partial upload only
moves the remainder, which is the difference between a resumable seed and one
that starts over every time it is interrupted.

    python scripts/seed_activities.py --zip /path/to/activities_export.zip
    python scripts/seed_activities.py --zip ... --limit 50   # a quick slice
    python scripts/seed_activities.py --zip ... --rows-only  # skip the artwork
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Someday
from app.services import storage
from app.taxonomy import canonical

settings = get_settings()

# The export's own vocabulary is nine categories and forty-seven subcategories.
# Both are carried through as-is: `category` for the chips people filter by,
# `domain` for the finer grain the feed uses to keep a page from turning into
# nine variations of the same afternoon.
IMAGE_DIR = "images_webp"


def title_of(name: str) -> str:
    """Sentence case, not title case.

    "mountain climbing" becomes "Mountain climbing". The catalog these join is
    written as things a person would say ("Attend a cold-brew tea workshop"),
    and Title Casing Every Word reads like a brochure next to it.
    """
    name = name.strip()
    return name[:1].upper() + name[1:] if name else name


def read_catalog(zip_path: Path, limit: int | None) -> list[dict]:
    """The rows, joined to the artwork member that actually exists in the zip."""
    with zipfile.ZipFile(zip_path) as archive:
        members = {
            Path(n).name: n
            for n in archive.namelist()
            if n.startswith(IMAGE_DIR + "/") and n.lower().endswith(".webp")
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "activities.db"
            db_path.write_bytes(archive.read("activities.db"))
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            sql = (
                "SELECT id, name, slug, category, subcategory, image_file "
                "FROM activities ORDER BY id"
            )
            if limit:
                sql += " LIMIT " + str(int(limit))
            rows = [dict(r) for r in connection.execute(sql)]
            connection.close()

    catalog: list[dict] = []
    missing: list[str] = []
    for row in rows:
        # `image_file` says .png; the zip ships .webp. Trust the stem, which is
        # `<zero-padded id>-<slug>`, and fall back to composing it from the row.
        stem = Path(row["image_file"]).stem or f"{row['id']:04d}-{row['slug']}"
        member = members.get(stem + ".webp")
        if member is None:
            missing.append(row["slug"])
            continue
        catalog.append({**row, "member": member})
    if missing:
        print(f"  ! {len(missing)} rows have no artwork in the zip, e.g. {missing[:3]}")
    return catalog


def upload_artwork(zip_path: Path, catalog: list[dict], workers: int) -> tuple[int, int]:
    """Push every WebP to `catalog/<slug>.webp`, skipping what is already there.

    Each worker opens the zip itself: a ZipFile handle is not safe to share
    across threads, and the alternative - reading every one of these into memory
    up front - is a quarter of a gigabyte held for no reason.
    """
    storage.ensure_catalog_bucket()
    client = storage._client()
    bucket = settings.s3_catalog_bucket

    existing: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="catalog/"):
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj["Size"]

    def put(entry: dict) -> str:
        key = "catalog/" + entry["slug"] + ".webp"
        with zipfile.ZipFile(zip_path) as archive:
            info = archive.getinfo(entry["member"])
            if existing.get(key) == info.file_size:
                return "skipped"
            body = archive.read(entry["member"])
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="image/webp",
            # Editorial artwork, identical for every account and never edited in
            # place - so it may be cached hard and for a long time.
            CacheControl="public, max-age=31536000, immutable",
        )
        return "uploaded"

    uploaded = skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(put, entry): entry for entry in catalog}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                if future.result() == "uploaded":
                    uploaded += 1
                else:
                    skipped += 1
            except ClientError as error:
                print(f"  ! {futures[future]['slug']}: {error}")
            if done % 250 == 0 or done == len(futures):
                print(f"  .. artwork {done}/{len(futures)}")
    return uploaded, skipped


async def upsert_rows(catalog: list[dict]) -> tuple[int, int]:
    """Rows by slug. A re-run refreshes the words and leaves the ids alone, so
    every keep, pass and plan already pointing at a someday still points at it."""
    created = updated = 0
    async with SessionLocal() as session:
        slugs = [entry["slug"] for entry in catalog]
        existing = {
            row.slug: row
            for row in (
                await session.scalars(select(Someday).where(Someday.slug.in_(slugs)))
            ).all()
        }
        for entry in catalog:
            row = existing.get(entry["slug"])
            fields = {
                "title": title_of(entry["name"]),
                "category": canonical(entry["category"]),
                "domain": entry["subcategory"],
                "party": "any",
                "register": "activity",
                "needs": [],
            }
            if row is None:
                session.add(Someday(slug=entry["slug"], **fields))
                created += 1
            else:
                changed = False
                for key, value in fields.items():
                    if getattr(row, key) != value:
                        setattr(row, key, value)
                        changed = True
                updated += int(changed)
        await session.commit()
    return created, updated


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rows-only", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    if not args.zip.exists():
        print(f"no such export: {args.zip}")
        return 1

    print(f"reading {args.zip.name} ...")
    catalog = read_catalog(args.zip, args.limit)
    print(f"  {len(catalog)} activities with artwork")

    created, updated = await upsert_rows(catalog)
    print(f"  rows: +{created} new, ~{updated} refreshed")

    if not args.rows_only:
        uploaded, skipped = upload_artwork(args.zip, catalog, args.workers)
        print(f"  artwork: +{uploaded} uploaded, ={skipped} already current")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
