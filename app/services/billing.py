"""Verify purchases with RevenueCat; client claims and webhook dates grant nothing."""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User

settings = get_settings()
PRODUCTS = {"gimik_plus", "gimik_plus:monthly", "gimik_plus_monthly"}


def _date(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Subscription dates must include a timezone")
    return parsed.astimezone(dt.UTC)


def verified_expiry(payload: dict, *, allow_sandbox: bool = False) -> dt.datetime | None:
    subscriber = payload["subscriber"]
    entitlement = subscriber["entitlements"].get("plus")
    if not entitlement:
        return None
    product = entitlement.get("product_identifier")
    if product not in PRODUCTS:
        return None
    subscription = subscriber["subscriptions"].get(product)
    if not subscription or subscription.get("store") not in {"app_store", "play_store"}:
        return None
    if subscription.get("is_sandbox") is not False and not allow_sandbox:
        return None
    if subscription.get("refunded_at"):
        return None
    # Cancellation retains the paid period. Expiry/refund revokes it; an active
    # billing grace period retains access until the store's explicit deadline.
    dates = [_date(entitlement.get("expires_date")),
             _date(entitlement.get("grace_period_expires_date"))]
    expiry = max((value for value in dates if value is not None), default=None)
    return expiry if expiry and expiry > dt.datetime.now(dt.UTC) else None


async def fetch_customer(user_id: uuid.UUID) -> dict:
    if not settings.revenuecat_api_key:
        raise HTTPException(503, "Subscriptions are not configured yet.")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"https://api.revenuecat.com/v1/subscribers/{user_id}",
                headers={"Authorization": f"Bearer {settings.revenuecat_api_key}"},
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        # No response body, receipt, Authorization header or customer data in logs.
        raise HTTPException(503, "Could not verify your subscription. Please try again.") from error


async def sync_customer(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    # Serialize refreshes before querying the provider. Duplicate and out-of-order
    # webhooks therefore apply current provider state, not stale event payloads.
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or not user.is_active:
        return None
    payload = await fetch_customer(user_id)
    allowed = {item.strip() for item in settings.revenuecat_sandbox_user_ids.split(",")}
    try:
        expiry = verified_expiry(payload, allow_sandbox=str(user_id) in allowed)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise HTTPException(503, "Could not verify your subscription. Please try again.") from error
    user.plus_until = expiry
    user.tier = "plus" if expiry else "free"
    await session.commit()
    return user
