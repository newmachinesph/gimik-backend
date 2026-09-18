"""Validate uploaded bytes and serve one metadata-free, upright WebP photo."""

from __future__ import annotations

import hashlib
import io
import warnings

from botocore.exceptions import ClientError
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from app.services import storage

register_heif_opener(thumbnails=False)

MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_SIDE = 2048
ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "image/heic-sequence", "image/heif-sequence", "image/avif", "image/gif",
    "image/tiff", "image/bmp", "image/x-ms-bmp",
}
FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "AVIF", "GIF", "TIFF", "BMP"}


class InvalidImage(ValueError):
    """A readable, safe error the API can show to the uploader."""


def normalized_key(key: str) -> str:
    # Stable for retries, separate from the writable presigned raw object.
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"{key.split('/', 1)[0]}/photos/{digest}.webp"


def normalize(data: bytes) -> bytes:
    if not data or len(data) > MAX_BYTES:
        raise InvalidImage("Choose an image smaller than 25 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=list(FORMATS)) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise InvalidImage("Choose an image with at most 25 megapixels")
                # Avatars are stills; never decode every animation/page in a file.
                source.seek(0)
                frame = ImageOps.exif_transpose(source)
                frame.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                if profile := frame.info.get("icc_profile"):
                    frame = ImageCms.profileToProfile(
                        frame, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                        ImageCms.createProfile("sRGB"), outputMode="RGBA" if "A" in frame.getbands() else "RGB",
                    )
                converted = frame.convert("RGBA" if "A" in frame.getbands() or "transparency" in frame.info else "RGB")
                # A fresh image excludes EXIF (including GPS), XMP and comments.
                clean = Image.new(converted.mode, converted.size)
                clean.paste(converted)
                result = io.BytesIO()
                clean.save(result, format="WEBP", quality=85, method=4)
                return result.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, ImageCms.PyCMSError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        if isinstance(error, InvalidImage):
            raise
        raise InvalidImage("This image is unsupported or damaged. Choose another photo.") from error


def process_upload(key: str) -> str:
    """Read at most MAX_BYTES, decode/validate, and write the canonical image."""
    try:
        obj = storage._client().get_object(Bucket=storage.settings.s3_bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            raise InvalidImage("Upload the photo before saving it") from error
        raise
    body = obj["Body"]
    try:
        if obj.get("ContentLength", 0) > MAX_BYTES:
            raise InvalidImage("Choose an image smaller than 25 MB")
        data = body.read(MAX_BYTES + 1)
    finally:
        body.close()
    normalized = normalize(data)
    target = normalized_key(key)
    storage._client().put_object(
        Bucket=storage.settings.s3_bucket, Key=target, Body=normalized,
        ContentType="image/webp", CacheControl="private, max-age=900",
    )
    return target
