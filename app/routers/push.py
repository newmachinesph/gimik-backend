"""Device registration for notifications.

Two endpoints and no cleverness. The interesting half - who gets told what, and
making sure a failed push never fails a write - lives in `app.services.push` and
at the call sites that actually know something happened.

Registration is idempotent and repeated on purpose: the client presents its
token on every sign-in and every cold start, because Expo can rotate one at any
time and a stale row is a person who quietly stops hearing from the app.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import User
from app.schemas import PushTokenIn
from app.services import push

router = APIRouter(prefix="/push", tags=["push"])


@router.put("/tokens", status_code=status.HTTP_204_NO_CONTENT)
async def register_token(
    payload: PushTokenIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Start reaching this person on this device."""
    await push.register(session, user.id, payload.token, payload.platform)


@router.delete("/tokens", status_code=status.HTTP_204_NO_CONTENT)
async def forget_token(
    payload: PushTokenIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Sign-out, from this handset.

    Deliberately keyed on the token rather than the account: signing out of a
    borrowed phone should stop that phone, not every device someone owns.
    """
    await push.forget(session, payload.token)
