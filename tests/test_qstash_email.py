from __future__ import annotations

import base64
import hashlib
import json
import time

import jwt

from app.config import get_settings
from app.routers import qstash_email


def _signed(body: bytes, key: str, url: str) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
    now = int(time.time())
    return jwt.encode(
        {"iss": "Upstash", "sub": url, "nbf": now - 5, "exp": now + 60, "body": digest},
        key,
        algorithm="HS256",
    )


async def test_qstash_callback_verifies_signature_before_sending(client, monkeypatch):
    settings = get_settings()
    url = "https://api.gimik.app/internal/qstash/email"
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "qstash_token", "test-token")
    monkeypatch.setattr(settings, "qstash_email_callback_url", url)
    current_key = "current-secret-key-with-32-bytes-minimum"
    next_key = "next-secret-key-with-32-bytes-minimum"
    monkeypatch.setattr(settings, "qstash_current_signing_key", current_key)
    monkeypatch.setattr(settings, "qstash_next_signing_key", next_key)
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(qstash_email, "deliver_email", lambda *args: sent.append(args))

    body = json.dumps(
        {"to": "person@example.com", "subject": "Subject", "text": "Body"}, separators=(",", ":")
    ).encode()
    endpoint = "/internal/qstash/email"

    unsigned = await client.post(endpoint, content=body)
    assert unsigned.status_code == 401
    altered = await client.post(
        endpoint,
        content=body + b" ",
        headers={"Upstash-Signature": _signed(body, current_key, url)},
    )
    assert altered.status_code == 401
    wrong_url = await client.post(
        endpoint,
        content=body,
        headers={"Upstash-Signature": _signed(body, current_key, "https://other.test")},
    )
    assert wrong_url.status_code == 401
    assert sent == []

    valid = await client.post(
        endpoint,
        content=body,
        headers={"Upstash-Signature": _signed(body, current_key, url)},
    )
    assert valid.status_code == 200
    assert sent == [("person@example.com", "Subject", "Body")]

    rotated = await client.post(
        endpoint,
        content=body,
        headers={"Upstash-Signature": _signed(body, next_key, url)},
    )
    assert rotated.status_code == 200
