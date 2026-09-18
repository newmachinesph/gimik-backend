from __future__ import annotations

import datetime as dt
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import current_user
from app.models import User
from app.services.billing import sync_customer
from app.services.ratelimit import Limit, limiter

router = APIRouter(prefix="/billing", tags=["billing"])
settings = get_settings()


class SubscriptionOut(BaseModel):
    tier: str
    plus_until: dt.datetime | None


@router.post("/sync", response_model=SubscriptionOut,
             dependencies=[Depends(limiter(Limit(10, 60, "billing")))])
async def sync_subscription(user: User = Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    # No app_user_id, tier, receipt or expiry accepted from the client.
    synced = await sync_customer(session, user.id)
    if synced is None:
        raise HTTPException(401, "Not authenticated")
    return SubscriptionOut(tier="plus" if synced.is_plus else "free", plus_until=synced.plus_until)


def verify_webhook(authorization: str | None = Header(default=None)) -> None:
    expected = settings.revenuecat_webhook_authorization
    if len(expected) < 32 or not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(401, "Invalid webhook authorization")


class WebhookEvent(BaseModel):
    type: str = Field(max_length=80)
    app_user_id: str | None = Field(default=None, max_length=1500)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    transferred_from: list[str] = Field(default_factory=list, max_length=20)
    transferred_to: list[str] = Field(default_factory=list, max_length=20)


class WebhookIn(BaseModel):
    event: WebhookEvent


@router.post("/webhook", dependencies=[Depends(verify_webhook)])
async def revenuecat_webhook(body: WebhookIn, session: AsyncSession = Depends(get_session)):
    event = body.event
    if event.type == "TEST":
        return {"ok": True}
    candidates = [event.app_user_id, *event.aliases, *event.transferred_from, *event.transferred_to]
    ids: set[uuid.UUID] = set()
    for candidate in candidates:
        try:
            ids.add(uuid.UUID(candidate))
        except (ValueError, TypeError, AttributeError):
            continue
    # Each commit is safe to retry. A partial failure returns 503 so RevenueCat
    # retries; already-applied recipients are simply rechecked.
    for user_id in sorted(ids):
        await sync_customer(session, user_id)
    return {"ok": True}
