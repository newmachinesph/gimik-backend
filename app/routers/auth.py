"""Register, login, refresh, and the profile behind the bearer."""

from __future__ import annotations

import logging
import re
import uuid

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import CREDENTIALS_ERROR, current_user
from app.models import GENDERS, User
from app.schemas import (
    ForgotPasswordIn,
    LoginIn,
    ProfileOut,
    ProfilePatch,
    RefreshIn,
    RegisterIn,
    ResetPasswordIn,
    TokenPair,
)
from app.security import create_token, decode_token, hash_password, verify_password
from app.services import email as email_service
from app.services import ratelimit, storage

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("gora.auth")


def _profile(user: User, *, include_email: bool) -> ProfileOut:
    from app.schemas import PhotoOut

    return ProfileOut(
        id=user.id,
        email=user.email if include_email else None,
        name=user.name,
        handle=user.handle,
        birthday=user.birthday,
        gender=user.gender,
        city=user.city,
        bio=user.bio,
        work=user.work,
        education=user.education,
        interests=user.interests,
        hometown=user.hometown,
        languages=user.languages,
        religion=user.religion,
        politics=user.politics,
        created_at=user.created_at,
        photos=[
            PhotoOut(id=photo.id, position=photo.position, url=storage.presign_download(photo.object_key))
            for photo in user.photos
        ],
    )


async def _unique_handle(session: AsyncSession, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "", name.lower()) or "gora"
    handle = base
    suffix = 0
    while await session.scalar(select(User.id).where(User.handle == handle)):
        suffix += 1
        handle = f"{base}{suffix}"
    return handle


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_UP))],
)
async def register(
    payload: RegisterIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    if payload.gender is not None and payload.gender not in GENDERS:
        raise HTTPException(status_code=422, detail="Unknown gender")
    existing = await session.scalar(select(User.id).where(User.email == payload.email.lower()))
    if existing:
        raise HTTPException(status_code=409, detail="That email already has an account")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        name=payload.name.strip(),
        handle=await _unique_handle(session, payload.name),
        birthday=payload.birthday,
        gender=payload.gender,
    )
    session.add(user)
    await session.commit()
    # After the response, on a worker thread: an account must not fail to exist
    # because a mail server was slow.
    email_service.send_welcome(tasks, to=user.email, name=user.name)
    return TokenPair(
        access_token=create_token(str(user.id), "access"),
        refresh_token=create_token(str(user.id), "refresh"),
    )


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    # One error for both a missing account and a wrong password: the response
    # must not confirm which emails have accounts.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="That email and password do not match")
    return TokenPair(
        access_token=create_token(str(user.id), "access"),
        refresh_token=create_token(str(user.id), "refresh"),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshIn, session: AsyncSession = Depends(get_session)) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, expect="refresh")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as error:
        raise CREDENTIALS_ERROR from error
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return TokenPair(
        access_token=create_token(str(user.id), "access"),
        refresh_token=create_token(str(user.id), "refresh"),
    )


@router.get("/me", response_model=ProfileOut)
async def me(user: User = Depends(current_user)) -> ProfileOut:
    return _profile(user, include_email=True)


@router.patch("/me", response_model=ProfileOut)
async def update_me(
    payload: ProfilePatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    if payload.city is not None:
        user.city = payload.city.strip() or None
    if payload.bio is not None:
        user.bio = payload.bio.strip()
    if payload.work is not None:
        user.work = payload.work.strip()
    if payload.education is not None:
        user.education = payload.education.strip()
    if payload.interests is not None:
        user.interests = payload.interests[:10]
    if payload.hometown is not None:
        user.hometown = payload.hometown.strip() or None
    if payload.languages is not None:
        user.languages = payload.languages[:12]
    if payload.religion is not None:
        user.religion = payload.religion.strip() or None
    if payload.politics is not None:
        user.politics = payload.politics.strip() or None
    await session.commit()
    refreshed = await session.scalar(
        select(User).options(selectinload(User.photos)).where(User.id == user.id)
    )
    assert refreshed is not None
    return _profile(refreshed, include_email=True)


@router.post(
    "/password/forgot",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def forgot_password(
    payload: ForgotPasswordIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Always 204, whether or not the address has an account.

    Anything else is an account-existence oracle: an attacker with a list of
    emails could sort it into customers and non-customers by reading status
    codes. The person who really owns the address finds out by receiving mail.
    """
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is not None:
        email_service.send_password_reset(
            tasks, to=user.email, name=user.name, token=create_token(str(user.id), "reset")
        )


@router.post(
    "/password/reset",
    response_model=TokenPair,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def reset_password(
    payload: ResetPasswordIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Spend the reset token, set the password, and sign them straight in.

    Returning a token pair means the person who just proved they own the inbox
    does not immediately have to type the password they only just chose.
    """
    try:
        claims = decode_token(payload.token, expect="reset")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link has expired. Ask for another.",
        ) from error

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That reset link is no longer valid")

    user.password_hash = hash_password(payload.password)
    await session.commit()
    # The useful notification is the one you did not expect.
    email_service.send_password_changed(tasks, to=user.email, name=user.name)
    return TokenPair(
        access_token=create_token(str(user.id), "access"),
        refresh_token=create_token(str(user.id), "refresh"),
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Actually delete the account, because the confirmation says it does.

    Every table hanging off a user is `ON DELETE CASCADE`, so the row going
    takes keeps, passes, invites, friendships, memberships, plans, journal
    entries and messages with it. The object store is not in the database and
    has to be swept by hand first - a deleted account that leaves its
    photographs behind has not been deleted.
    """
    keys = [photo.object_key for photo in user.photos]
    await session.delete(user)
    await session.commit()
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception:  # noqa: BLE001 - a stranded object must not fail the deletion
            log.warning("could not remove %s for deleted user %s", key, user.id)
