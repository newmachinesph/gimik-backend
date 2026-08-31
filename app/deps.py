"""Request dependencies. The only place a bearer token becomes a User."""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import User
from app.security import decode_token, token_is_current

bearer = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise CREDENTIALS_ERROR
    try:
        payload = decode_token(credentials.credentials, expect="access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as error:
        raise CREDENTIALS_ERROR from error

    user = await session.scalar(
        select(User).options(selectinload(User.photos)).where(User.id == user_id)
    )
    # A token outliving its account must not authenticate.
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    # Nor one outliving the password it was issued against. This is the check
    # that makes signing out, changing a password and spending a reset link
    # mean something to a token already in the wild.
    if not token_is_current(payload, user.token_version):
        raise CREDENTIALS_ERROR
    return user
