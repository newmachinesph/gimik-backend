"""S3-compatible object storage, ported from v1 and unchanged in spirit.

MinIO now, Cloudflare R2 later. Both speak S3, so the move is four environment
variables and no code - provided nothing here reaches for an AWS-only feature.
Two rules keep that true: address buckets by path (R2 does not do virtual-host
style for custom domains), and sign with SigV4 against region `auto`.

User media is private and served by short-lived presigned URL. Catalog artwork
is editorial content shown to every account - the v1 stack already seeded the
`gora-catalog` bucket with `catalog/<slug>.webp` for every catalog concept and
made it publicly readable, and this API simply hands out those plain URLs.
"""

from __future__ import annotations

import json
import logging
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("gimik.storage")

DEFAULT_EXPIRY_SECONDS = 900


def _build(endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


@lru_cache
def _client() -> Any:
    """Talks to the store. Inside Docker that host is the compose/host alias, not localhost."""
    return _build(settings.s3_endpoint_url)


@lru_cache
def _signing_client() -> Any:
    """Signs URLs for whoever will open them - a phone, not this container.

    SigV4 covers the Host header, so a URL signed for an internal hostname
    cannot be rewritten to a reachable one afterwards. It has to be signed
    against the public endpoint from the start.
    """
    return _build(settings.s3_public_url or settings.s3_endpoint_url)


def object_key(user_id: str, filename: str) -> str:
    """Namespaced by owner, so a listing can never span two people's media."""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"{user_id}/{uuid.uuid4()}.{suffix}"


def ensure_bucket() -> None:
    """Idempotent. MinIO starts empty; R2 buckets are made in the dashboard."""
    client = _client()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket"}:
            raise
        client.create_bucket(Bucket=settings.s3_bucket)


def ensure_catalog_bucket() -> None:
    """The catalog bucket is expected to exist already, seeded by the v1 stack.

    When it does not (a fresh machine), it is created empty and made publicly
    readable so a later sync can fill it. On R2 bucket policies are rejected;
    public access is configured in the dashboard instead, so a rejection is
    logged rather than fatal.
    """
    client = _client()
    try:
        client.head_bucket(Bucket=settings.s3_catalog_bucket)
        return
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket"}:
            raise
    client.create_bucket(Bucket=settings.s3_catalog_bucket)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{settings.s3_catalog_bucket}/*"],
            }
        ],
    }
    try:
        client.put_bucket_policy(
            Bucket=settings.s3_catalog_bucket, Policy=json.dumps(policy)
        )
    except ClientError as error:
        log.warning(
            "could not set the public-read policy on %s: %s",
            settings.s3_catalog_bucket,
            error.response.get("Error", {}).get("Code"),
        )


def catalog_url(slug: str) -> str:
    """The plain, cacheable URL of a catalog artwork - no signature, by design.

    MinIO addresses buckets by path, so the bucket has to be a URL segment. R2's
    public hostname (r2.dev or a custom domain) is already rooted at a bucket.
    An explicit catalog root also supports CDNs independently of S3 signing.
    """
    from urllib.parse import urlsplit

    if settings.s3_catalog_public_url:
        return f"{settings.s3_catalog_public_url.rstrip('/')}/catalog/{slug}.webp"
    base = (settings.s3_public_url or settings.s3_endpoint_url).rstrip("/")
    public_host = urlsplit(base).hostname or ""
    endpoint_host = urlsplit(settings.s3_endpoint_url).hostname or ""
    r2_public_domain = bool(settings.s3_public_url) and endpoint_host.endswith(
        ".r2.cloudflarestorage.com"
    ) and public_host != endpoint_host
    if public_host.endswith(".r2.dev") or r2_public_domain:
        return f"{base}/catalog/{slug}.webp"
    return f"{base}/{settings.s3_catalog_bucket}/catalog/{slug}.webp"


def presign_upload(key: str, content_type: str, expires: int = DEFAULT_EXPIRY_SECONDS) -> str:
    return _signing_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def presign_download(key: str, expires: int = DEFAULT_EXPIRY_SECONDS) -> str:
    return _signing_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires,
    )


def delete_object(key: str) -> None:
    _client().delete_object(Bucket=settings.s3_bucket, Key=key)
