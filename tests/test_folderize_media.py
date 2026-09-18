import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.models import JournalEntry, UserPhoto
from app.services import storage
from scripts import folderize_media
from scripts.folderize_media import destination, verify


def test_migration_only_moves_uuid_roots():
    owner = str(uuid.uuid4())
    assert destination(f"{owner}/photos/a.webp") == f"users/{owner}/photos/a.webp"
    for key in ["catalog/a.webp", f"users/{owner}/a.webp", "other/a.webp", f"{owner}/", owner]:
        assert destination(key) is None


def test_copy_verification_fails_closed():
    expected = {"etag": "abc", "size": 42}
    verify({"ETag": "abc", "ContentLength": 42}, expected)
    for info in [None, {"ETag": "different", "ContentLength": 42}, {"ETag": "abc", "ContentLength": 3}]:
        with pytest.raises(RuntimeError):
            verify(info, expected)


@pytest.mark.parametrize("collision", [False, True])
async def test_migration_copy_switch_and_delayed_cleanup(client, make_user, somedays, monkeypatch, tmp_path, collision):
    headers = await make_user("MigrationQA")
    owner = (await client.get("/auth/me", headers=headers)).json()["id"]
    source = f"{owner}/photos/fixture.webp"
    target = f"users/{source}"
    async with client.sessionmaker() as session:
        session.add(UserPhoto(user_id=uuid.UUID(owner), object_key=source, position=0))
        session.add(JournalEntry(user_id=uuid.UUID(owner), someday_id=uuid.UUID(somedays["reef"]), photo_key=source))
        await session.commit()
    objects = {source: {"ETag": "abc", "ContentLength": 42}, "catalog/art.webp": {"ETag": "catalog", "ContentLength": 9}}
    if collision:
        objects[target] = {"ETag": "different", "ContentLength": 42}

    class FakeS3:
        def get_paginator(self, operation):
            return self

        def paginate(self, **kwargs):
            return [{"Contents": [{"Key": k, "ETag": v["ETag"], "Size": v["ContentLength"]} for k, v in objects.items()]}]

        def head_object(self, *, Bucket, Key):
            if Key not in objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return objects[Key]

        def copy_object(self, *, Bucket, Key, CopySource, CopySourceIfMatch, MetadataDirective):
            assert objects[CopySource["Key"]]["ETag"] == CopySourceIfMatch
            objects[Key] = objects[CopySource["Key"]].copy()

        def delete_object(self, *, Bucket, Key):
            assert Key == source
            del objects[Key]

    monkeypatch.setattr(storage, "_client", FakeS3)
    monkeypatch.setattr(folderize_media, "SessionLocal", client.sessionmaker)
    args = SimpleNamespace(bucket=storage.settings.s3_bucket, manifest=str(tmp_path / "migration.json"), apply=False, cleanup=False)
    await folderize_media.migrate(args)
    assert source in objects
    args.apply = True
    if collision:
        with pytest.raises(RuntimeError, match="changed"):
            await folderize_media.migrate(args)
        async with client.sessionmaker() as session:
            assert (await session.scalar(select(UserPhoto))).object_key == source
        assert source in objects
        return
    await folderize_media.migrate(args)
    async with client.sessionmaker() as session:
        assert (await session.scalar(select(UserPhoto))).object_key == target
        assert (await session.scalar(select(JournalEntry))).photo_key == target
    assert source in objects and target in objects
    args.apply, args.cleanup = False, True
    with pytest.raises(RuntimeError, match="expired"):
        await folderize_media.migrate(args)
    now = folderize_media.time.time()
    monkeypatch.setattr(folderize_media.time, "time", lambda: now + 1000)
    await folderize_media.migrate(args)
    assert source not in objects and target in objects and "catalog/art.webp" in objects
