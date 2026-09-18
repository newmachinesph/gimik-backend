"""Move UUID-root media into users/, without touching catalog or bucket access.

Dry-run by default. Apply copies and verifies objects before a DB transaction.
Cleanup is separate and waits for previously issued download URLs to expire.
The local manifest records exact keys; surviving destination copies allow recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import JournalEntry, User, UserPhoto
from app.services import storage


def destination(key: str) -> str | None:
    root, separator, tail = key.partition("/")
    try:
        valid = str(uuid.UUID(root)) == root
    except ValueError:
        return None
    return f"users/{key}" if valid and separator and tail else None


def head(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def verify(info, expected):
    if info is None or info["ETag"] != expected["etag"] or info["ContentLength"] != expected["size"]:
        raise RuntimeError("Object missing or changed; no source will be removed")


async def references(session):
    rows = []
    for model, attr in [(UserPhoto, "object_key"), (JournalEntry, "photo_key")]:
        for row in (await session.scalars(select(model))).all():
            key = getattr(row, attr)
            if key and destination(key):
                if key.split("/", 1)[0] != str(row.user_id):
                    raise RuntimeError("Reference owner mismatch")
                rows.append((row, attr, key))
    return rows


async def migrate(args):
    bucket = storage.settings.s3_bucket
    if bucket != args.bucket:
        raise RuntimeError("Configured bucket does not match --bucket")
    client = storage._client()
    manifest_path = Path(args.manifest)
    if args.cleanup:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["bucket"] != bucket or manifest["endpoint"] != storage.settings.s3_endpoint_url:
            raise RuntimeError("Manifest storage mismatch")
        if time.time() < manifest["cleanup_after"]:
            raise RuntimeError("Old signed URLs have not expired yet")
        async with SessionLocal() as session:
            refs = await references(session)
            if refs:
                raise RuntimeError("Legacy DB references still exist; rerun migration first")
        # Preflight every entry before deleting any source. No prefix-wide deletes.
        for obj in manifest["objects"]:
            if destination(obj["source"]) != obj["target"]:
                raise RuntimeError("Unsafe manifest target")
            verify(head(client, bucket, obj["target"]), obj)
            source = head(client, bucket, obj["source"])
            if source:
                verify(source, obj)
        removed = 0
        for obj in manifest["objects"]:
            if head(client, bucket, obj["source"]):
                client.delete_object(Bucket=bucket, Key=obj["source"])
                removed += 1
        print(json.dumps({"removed_legacy_copies": removed, "destination_copies_preserved": len(manifest["objects"])}))
        return

    objects = []
    catalog_count = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith("catalog/"):
                catalog_count += 1
            target = destination(key)
            if target:
                objects.append({"source": key, "target": target, "etag": obj["ETag"], "size": obj["Size"]})
    async with SessionLocal() as session:
        users = {str(id_) for id_ in (await session.scalars(select(User.id))).all()}
        if any(obj["source"].split("/", 1)[0] not in users for obj in objects):
            raise RuntimeError("Unknown owner in legacy root; inspect before moving")
        refs = await references(session)
        keys = {obj["source"] for obj in objects}
        if any(key not in keys for _, _, key in refs):
            raise RuntimeError("Referenced source missing from bucket")
        print(json.dumps({"bucket": bucket, "legacy_objects": len(objects), "database_references": len(refs), "catalog_untouched": catalog_count, "apply": args.apply}))
        if not args.apply or not objects:
            return
        if manifest_path.exists():
            raise RuntimeError("Use a fresh manifest path; never overwrite a rollback record")
        manifest = {
            "bucket": bucket, "endpoint": storage.settings.s3_endpoint_url,
            "objects": objects,
            "references": [{"table": row.__tablename__, "id": str(row.id), "field": attr, "source": key, "target": destination(key)} for row, attr, key in refs],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        # Copy first. Any failure leaves the database and source objects intact.
        for obj in objects:
            target = head(client, bucket, obj["target"])
            if target is None:
                client.copy_object(
                    Bucket=bucket, Key=obj["target"],
                    CopySource={"Bucket": bucket, "Key": obj["source"]},
                    CopySourceIfMatch=obj["etag"], MetadataDirective="COPY",
                )
            verify(head(client, bucket, obj["target"]), obj)
        # Lock and re-read media rows so concurrent updates cannot be overwritten.
        await session.rollback()
        for model in (UserPhoto, JournalEntry):
            await session.execute(select(model).with_for_update())
        refs = await references(session)
        if any(key not in keys for _, _, key in refs):
            raise RuntimeError("Concurrent legacy upload; retry with a fresh manifest")
        for row, attr, key in refs:
            setattr(row, attr, destination(key))
        await session.commit()
        manifest["cleanup_after"] = time.time() + storage.DEFAULT_EXPIRY_SECONDS + 5
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps({"copied_and_verified": len(objects), "updated_references": len(refs), "cleanup_wait_seconds": storage.DEFAULT_EXPIRY_SECONDS + 5}))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--manifest", default=".local/folderize-media.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--cleanup", action="store_true")
    try:
        await migrate(parser.parse_args())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
