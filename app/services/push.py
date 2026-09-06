"""Reaching someone whose app is closed.

Ably delivers to a live connection, which covers the case where both people are
looking at Gora in the same moment - and almost nothing else. The product's
whole loop waits on somebody answering an invite, and an invite that lapses
after a week is a poor thing to leave sitting in an app nobody has opened.

So: Expo's push service, which is the right dependency here rather than talking
to FCM and APNs separately - the client is an Expo app and already owns a token
per device.

Two rules, both borrowed from the email service next door:

**Never block the request.** A push is a courtesy alongside a write, not part of
it. Everything here is called from a BackgroundTask, and a failure is logged
rather than raised - nobody's invite should fail to send because a push gateway
had a bad minute.

**Prune what the gateway rejects.** Tokens die: apps are uninstalled, devices
are wiped. Expo says so in the receipt with `DeviceNotRegistered`, and a token
that is told to go away and does not is a row that gets retried forever.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PushToken

log = logging.getLogger("gora.push")

EXPO_ENDPOINT = "https://exp.host/--/api/v2/push/send"
# Expo accepts up to 100 messages per request.
BATCH = 100
TIMEOUT_SECONDS = 8.0


async def register(session: AsyncSession, user_id: uuid.UUID, token: str, platform: str) -> None:
    """Remember a device, or move it to whoever is signed in now.

    A token identifies a handset, not a person, so the same one arriving for a
    second account means the first signed out of that phone - the row moves
    rather than duplicating, or the previous user keeps receiving the new one's
    notifications.
    """
    existing = await session.scalar(select(PushToken).where(PushToken.token == token))
    if existing is None:
        session.add(PushToken(user_id=user_id, token=token, platform=platform))
    else:
        existing.user_id = user_id
        existing.platform = platform
        existing.seen_at = dt.datetime.now(dt.UTC)
    await session.commit()


async def forget(session: AsyncSession, token: str) -> None:
    """Sign-out. This handset stops being a way to reach this person."""
    await session.execute(delete(PushToken).where(PushToken.token == token))
    await session.commit()


async def send_to(
    session: AsyncSession,
    user_ids: list[uuid.UUID],
    *,
    title: str,
    body: str,
    data: dict | None = None,
) -> int:
    """Push to every device of everyone named. Returns how many were accepted.

    Callers pass a list because the interesting notifications are plural - a
    message goes to everyone in a thread except its author. Sending nothing is a
    perfectly ordinary outcome: plenty of accounts have never opened the app on
    a device that accepted notifications.
    """
    if not user_ids:
        return 0
    rows = (
        await session.scalars(select(PushToken).where(PushToken.user_id.in_(set(user_ids))))
    ).all()
    if not rows:
        return 0

    messages = [
        {
            "to": row.token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
            "channelId": "default",
        }
        for row in rows
    ]

    sent = 0
    dead: list[str] = []
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        for start in range(0, len(messages), BATCH):
            chunk = messages[start : start + BATCH]
            try:
                response = await http.post(EXPO_ENDPOINT, json=chunk)
                response.raise_for_status()
                receipts = response.json().get("data", [])
            except Exception as error:  # noqa: BLE001 - a push must never fail a write
                log.warning("push batch failed: %s", error)
                continue
            for message, receipt in zip(chunk, receipts, strict=False):
                if receipt.get("status") == "ok":
                    sent += 1
                    continue
                if receipt.get("details", {}).get("error") == "DeviceNotRegistered":
                    dead.append(message["to"])
                else:
                    log.warning("push rejected: %s", receipt.get("message"))

    if dead:
        await session.execute(delete(PushToken).where(PushToken.token.in_(dead)))
        await session.commit()
        log.info("pruned %d dead push tokens", len(dead))
    return sent
