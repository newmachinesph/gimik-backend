"""Ably: scoped tokens for clients, fire-and-forget publishes from the server.

The root API key never leaves this process. A client asks for a token, gets one
that can only subscribe to its own channels, and it expires within the hour -
so a token lifted off a device is worth very little and nothing at all for long.

Publishing goes over Ably's REST endpoint rather than a persistent connection:
the API emits a handful of events per request at most, and a stateless POST
cannot leak a socket per worker. A publish that fails is logged and dropped -
realtime is a courtesy on top of the database, never the record of what
happened, and no request should fail because a notification did not land.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()
log = logging.getLogger("gora.realtime")

REST_BASE = "https://rest.ably.io"
PUBLISH_TIMEOUT_SECONDS = 5.0


def _key_parts() -> tuple[str, str]:
    """`appId.keyId:keySecret` - the name is public, the secret is not."""
    name, _, secret = settings.ably_api_key.partition(":")
    return name, secret


def user_channel(user_id: uuid.UUID | str) -> str:
    """Everything one person is allowed to hear about, on one channel."""
    return f"user:{user_id}"


def thread_channel(thread_id: uuid.UUID | str) -> str:
    return f"thread:{thread_id}"


def token_request(user_id: uuid.UUID | str, thread_ids: list[str] | None = None) -> dict[str, Any]:
    """A signed TokenRequest for this user, and nothing wider.

    Signed here rather than fetched from Ably: the HMAC is over a canonical
    string we can build ourselves, so issuing a token costs no network round
    trip and cannot fail because Ably is briefly unreachable.
    """
    key_name, key_secret = _key_parts()
    subscribe_only = {"subscribe", "presence", "history"}
    capability: dict[str, list[str]] = {
        user_channel(user_id): sorted(subscribe_only),
    }
    for thread_id in thread_ids or []:
        # Publishing to a thread goes through the API so it lands in Postgres;
        # the channel itself is read-only to the client.
        capability[thread_channel(thread_id)] = sorted(subscribe_only)

    request: dict[str, Any] = {
        "keyName": key_name,
        "ttl": settings.ably_token_ttl_seconds * 1000,
        "capability": json.dumps(capability, separators=(",", ":"), sort_keys=True),
        "clientId": str(user_id),
        "timestamp": int(time.time() * 1000),
        "nonce": secrets.token_hex(8),
    }
    canonical = "\n".join([
        request["keyName"],
        str(request["ttl"]),
        request["capability"],
        request["clientId"],
        str(request["timestamp"]),
        request["nonce"],
    ]) + "\n"
    request["mac"] = base64.b64encode(
        hmac.new(key_secret.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()
    return request


async def publish(channel: str, name: str, data: dict[str, Any]) -> None:
    """Best effort. A dropped event never fails the request that caused it."""
    if not settings.realtime_enabled:
        return
    key_name, key_secret = _key_parts()
    auth = base64.b64encode(f"{key_name}:{key_secret}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=PUBLISH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{REST_BASE}/channels/{channel}/messages",
                headers={"Authorization": f"Basic {auth}"},
                json={"name": name, "data": data},
            )
        if response.status_code >= 400:
            log.warning("ably publish %s -> %s: %s", channel, response.status_code, response.text[:200])
    except Exception:
        log.exception("ably publish to %s failed", channel)
