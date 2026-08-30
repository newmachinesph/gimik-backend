"""The client's way onto a realtime channel: a token, never the key."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import current_user
from app.models import Thread, ThreadParticipant, User
from app.services import ratelimit, realtime

router = APIRouter(prefix="/realtime", tags=["realtime"])
settings = get_settings()


@router.post("/token", dependencies=[Depends(ratelimit.limiter(ratelimit.GENERAL))])
async def token(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """A short-lived Ably token, scoped to this person's own channels.

    The capability is computed from the threads they are actually in, so it
    cannot be widened by asking differently - the request body is ignored on
    purpose.
    """
    if not settings.realtime_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Realtime is not configured",
        )
    thread_ids = (
        await session.scalars(
            select(Thread.id)
            .join(ThreadParticipant, ThreadParticipant.thread_id == Thread.id)
            .where(ThreadParticipant.user_id == user.id)
        )
    ).all()
    return realtime.token_request(user.id, [str(tid) for tid in thread_ids])
