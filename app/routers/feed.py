"""The deck endpoint: what to ask this person about next.

Thin on purpose - the ranking lives in `app.services.feed`, and this file is the
HTTP shape around it. One decision is made here rather than there: the deck does
not take a cursor.

Paging a deck by offset is subtly wrong. The pool is "everything you have not
decided on", and it shrinks as you swipe - so page two at offset 20 skips
whatever left the pool while page one was on screen. Instead every call returns
the best undecided cards right now, and deciding is what advances the deck. The
client refills when it runs low and drops ids it already holds. Skip-free by
construction, with no cursor to keep valid.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import User
from app.routers.common import someday_out
from app.schemas import FeedItemOut, FeedOut
from app.services import feed as feed_service

router = APIRouter(tags=["feed"])

# Twenty is about three minutes of swiping - enough that a refill is rare mid
# session, small enough that the ranking stays fresh as taste moves.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


@router.get("/feed", response_model=FeedOut)
async def deck(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    category: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FeedOut:
    """The next cards, ranked, spread across categories, with their reasons."""
    page = await feed_service.build_feed(session, user, limit=limit, category=category)
    return FeedOut(
        items=[
            FeedItemOut(
                someday=someday_out(item.someday),
                pool=item.pool,
                reasons=item.reasons,
            )
            for item in page.items
        ],
        exhausted=page.exhausted,
        pools=page.pools,
    )
