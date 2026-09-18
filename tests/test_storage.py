import pytest

from app.services import storage


def test_user_media_root():
    key = storage.object_key("owner", "iphone.HEIC")
    assert key.startswith("users/owner/")
    assert key.endswith(".heic")
    assert storage.upload_key_owner(key) == "owner"
    assert storage.upload_key_owner("owner/legacy.jpg") == "owner"
    assert storage.object_key("owner", "bad.ext/path").endswith(".bin")


@pytest.mark.parametrize("key", ["users/owner/photos/a.webp", "owner/photos/a.webp", "users/owner/", "owner/..", "owner/a\\b"])
def test_upload_key_rejects_non_raw_objects(key):
    with pytest.raises(ValueError):
        storage.upload_key_owner(key)


@pytest.mark.parametrize(
    ("endpoint", "public", "expected"),
    [
        ("http://minio:9000", "http://localhost:9000", "http://localhost:9000"),
        ("http://minio:9000", "", "http://minio:9000"),
        ("https://account.r2.cloudflarestorage.com", "https://cdn.gimik.app", "https://account.r2.cloudflarestorage.com"),
        ("https://account.r2.cloudflarestorage.com", "https://pub-test.r2.dev", "https://account.r2.cloudflarestorage.com"),
    ],
)
def test_signing_uses_s3_api_not_r2_cdn(monkeypatch, endpoint, public, expected):
    monkeypatch.setattr(storage.settings, "s3_endpoint_url", endpoint)
    monkeypatch.setattr(storage.settings, "s3_public_url", public)
    monkeypatch.setattr(storage, "_build", lambda url: url)
    storage._signing_client.cache_clear()
    try:
        assert storage._signing_client() == expected
    finally:
        storage._signing_client.cache_clear()


@pytest.mark.parametrize(
    ("endpoint", "public", "catalog_root", "expected"),
    [
        ("http://minio:9000", "http://localhost:9000", "", "http://localhost:9000/gimik"),
        ("http://minio:9000", "", "", "http://minio:9000/gimik"),
        ("https://account.r2.cloudflarestorage.com", "https://pub-test.r2.dev/", "", "https://pub-test.r2.dev"),
        ("https://account.r2.cloudflarestorage.com", "https://cdn.gimik.app/", "", "https://cdn.gimik.app"),
        ("https://account.r2.cloudflarestorage.com", "", "", "https://account.r2.cloudflarestorage.com/gimik"),
        ("https://account.r2.cloudflarestorage.com", "https://account.r2.cloudflarestorage.com", "", "https://account.r2.cloudflarestorage.com/gimik"),
        ("http://minio:9000", "http://localhost:9000", "https://cdn.example.com/assets/", "https://cdn.example.com/assets"),
    ],
)
def test_catalog_url(monkeypatch, endpoint, public, catalog_root, expected):
    monkeypatch.setattr(storage.settings, "s3_endpoint_url", endpoint)
    monkeypatch.setattr(storage.settings, "s3_public_url", public)
    monkeypatch.setattr(storage.settings, "s3_catalog_public_url", catalog_root)
    monkeypatch.setattr(storage.settings, "s3_catalog_bucket", "gimik")
    assert storage.catalog_url("anime-convention-attending") == (
        f"{expected}/catalog/anime-convention-attending.webp"
    )
