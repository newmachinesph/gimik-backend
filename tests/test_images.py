from __future__ import annotations

import io
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from app.services import images, storage

# The API suite fakes the storage boundary; retain the real processor for unit tests.
process_upload = images.process_upload


def fixture_bytes(fmt="PNG", size=(80, 60), **options):
    out = io.BytesIO()
    Image.new("RGB", size, (240, 85, 25)).save(out, format=fmt, **options)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF", "AVIF", "GIF", "TIFF", "BMP"])
def test_supported_formats_become_webp(fmt):
    with Image.open(io.BytesIO(images.normalize(fixture_bytes(fmt)))) as photo:
        assert photo.format == "WEBP"
        assert photo.size == (80, 60)
        assert not photo.getexif()
        assert photo.n_frames == 1


def test_orientation_metadata_and_resize():
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees, like a portrait iPhone photo.
    exif[270] = "private camera metadata"
    data = fixture_bytes("JPEG", (3000, 2000), exif=exif)
    with Image.open(io.BytesIO(images.normalize(data))) as photo:
        assert photo.size == (1365, 2048)
        assert not photo.getexif()
        assert "xmp" not in photo.info


def test_animation_becomes_one_still():
    out = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(
        out, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (20, 20), "blue")], duration=100,
    )
    with Image.open(io.BytesIO(images.normalize(out.getvalue()))) as photo:
        assert photo.n_frames == 1


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg></svg>", b"\x89PNG\r\n\x1a\ntruncated"])
def test_rejects_fake_or_broken_images(data):
    with pytest.raises(images.InvalidImage):
        images.normalize(data)


def test_rejects_large_bytes_and_dimensions(monkeypatch):
    monkeypatch.setattr(images, "MAX_BYTES", 10)
    with pytest.raises(images.InvalidImage, match="25 MB"):
        images.normalize(b"x" * 11)
    monkeypatch.setattr(images, "MAX_BYTES", 10000)
    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    with pytest.raises(images.InvalidImage, match="megapixels"):
        images.normalize(fixture_bytes())


def test_storage_reads_bounded_bytes_and_sets_real_mime(monkeypatch):
    source = io.BytesIO(fixture_bytes("HEIF"))
    client = Mock()
    client.get_object.return_value = {"Body": source, "ContentLength": len(source.getvalue())}
    monkeypatch.setattr(storage, "_client", lambda: client)
    key = "owner/test.heic"
    assert process_upload(key) == images.normalized_key(key)
    assert source.closed
    written = client.put_object.call_args.kwargs
    assert written["ContentType"] == "image/webp"
    assert written["Key"] != key
    with Image.open(io.BytesIO(written["Body"])) as photo:
        assert photo.format == "WEBP"


def test_missing_uploaded_object_is_validation_error(monkeypatch):
    client = Mock()
    client.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(storage, "_client", lambda: client)
    with pytest.raises(images.InvalidImage, match="Upload"):
        process_upload("owner/missing.heic")
    client.put_object.assert_not_called()


async def test_claim_validates_and_retry_is_idempotent(client, make_user, monkeypatch):
    headers = await make_user("PhotoQA")
    ticket = await client.post("/media/presign", json={"filename": "iphone.heic", "content_type": "image/heic"}, headers=headers)
    assert ticket.status_code == 200
    key = ticket.json()["key"]
    processor = Mock(side_effect=images.InvalidImage("Damaged image"))
    monkeypatch.setattr(images, "process_upload", processor)
    refused = await client.post("/media/photos", json={"key": key}, headers=headers)
    assert refused.status_code == 422
    assert (await client.get("/auth/me", headers=headers)).json()["photos"] == []
    processor.side_effect = images.normalized_key
    first = await client.post("/media/photos", json={"key": key}, headers=headers)
    retry = await client.post("/media/photos", json={"key": key}, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"]
    assert first.json()["url"].endswith(".webp")
    assert len((await client.get("/auth/me", headers=headers)).json()["photos"]) == 1


@pytest.mark.parametrize("mime", sorted(images.ALLOWED_TYPES))
async def test_presign_all_supported_types(client, make_user, mime):
    headers = await make_user("PhotoQA")
    response = await client.post("/media/presign", json={"filename": "image.file", "content_type": mime}, headers=headers)
    assert response.status_code == 200


async def test_journal_photos_are_owned_and_normalized(client, make_user, somedays):
    headers = await make_user("JournalQA")
    route = f"/somedays/{somedays['reef']}/journal"
    refused = await client.post(route, json={"body": "", "photo_key": "another-owner/private.heic"}, headers=headers)
    assert refused.status_code == 403
    ticket = await client.post("/media/presign", json={"filename": "journal.heic", "content_type": "image/heic"}, headers=headers)
    saved = await client.post(route, json={"body": "", "photo_key": ticket.json()["key"]}, headers=headers)
    assert saved.status_code == 201
    assert saved.json()["photo_url"].endswith(".webp")
    assert (await client.get(route, headers=headers)).json()[0]["id"] == saved.json()["id"]
