import pytest

from app.services import storage


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
