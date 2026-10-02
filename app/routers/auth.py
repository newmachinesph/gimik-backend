"""Register, login, refresh, and the profile behind the bearer."""

from __future__ import annotations

import logging
import re
import uuid

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import CREDENTIALS_ERROR, current_user
from app.models import GENDERS, User
from app.schemas import (
    AppleSignInIn,
    EmailIn,
    ForgotPasswordIn,
    GoogleSignInIn,
    LoginIn,
    ProfileOut,
    ProfilePatch,
    RefreshIn,
    RegisterIn,
    ResetPasswordIn,
    TokenPair,
    VerifyEmailIn,
)
from app.security import (
    create_token,
    decode_token,
    hash_password,
    token_is_current,
    verify_password,
)
from app.services import apple_auth, google_auth, moderation, ratelimit, storage
from app.services import email as email_service

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("gimik.auth")


def _profile(user: User, *, include_email: bool) -> ProfileOut:
    from app.schemas import PhotoOut

    return ProfileOut(
        id=user.id,
        email=user.email if include_email else None,
        email_verified=user.email_verified if include_email else False,
        tier=("plus" if user.is_plus else "free") if include_email else "free",
        plus_until=user.plus_until if (include_email and user.is_plus) else None,
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


def _pair_for(user: User) -> TokenPair:
    """Both tokens, stamped with the account's current version.

    One place rather than five, because a pair minted with a stale version is
    a session that dies on its next request - a bug that would look like a
    flaky login rather than what it is.
    """
    return TokenPair(
        access_token=create_token(str(user.id), "access", user.token_version),
        refresh_token=create_token(str(user.id), "refresh", user.token_version),
    )


async def _unique_handle(session: AsyncSession, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "", name.lower()) or "gimik"
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
    moderation.screen(payload.name)
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
    # Verification is not a gate: the account works from this moment. It is
    # sent now because the one time someone is certainly looking at their inbox
    # is straight after signing up.
    email_service.send_verify_email(
        tasks,
        to=user.email,
        name=user.name,
        token=create_token(str(user.id), "verify", user.token_version),
    )
    return _pair_for(user)


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def login(payload: LoginIn, session: AsyncSession = Depends(get_session)) -> TokenPair:
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    # One error for both a missing account and a wrong password: the response
    # must not confirm which emails have accounts.
    # A Google-only account has no password_hash and lands here too, which is
    # the same answer for the same reason: say nothing about who exists.
    if (
        user is None
        or user.password_hash is None
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="That email and password do not match")
    return _pair_for(user)


@router.post(
    "/google",
    response_model=TokenPair,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def google_sign_in(
    payload: GoogleSignInIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Sign in - or sign up - with a Google ID token.

    Three cases, tried in this order: a Google account seen before, a Google
    account whose verified address already has a password account here, and
    somebody new. The middle case links rather than duplicates, because the
    alternative is one person with two accounts and half their somedays in each.

    Linking is only safe because the verifier refuses tokens whose email Google
    has not confirmed; without that check this would be a way to claim anyone's
    account by putting their address on a fresh Google profile.
    """
    try:
        claims = await run_in_threadpool(google_auth.verify_id_token, payload.id_token)
    except google_auth.GoogleAuthError as exc:
        # The detail stays vague on purpose; the reason goes to the log.
        log.info("Google sign-in rejected: %s", exc)
        raise HTTPException(
            status_code=401, detail="That Google sign-in could not be verified"
        ) from exc

    user = await session.scalar(select(User).where(User.google_sub == claims["sub"]))
    if user is not None:
        return _pair_for(user)

    user = await session.scalar(select(User).where(User.email == claims["email"]))
    if user is not None:
        user.google_sub = claims["sub"]
        # Google has just proved the address, so the flag can only improve.
        user.email_verified = True
        await session.commit()
        return _pair_for(user)

    # A provider-supplied name is not refused - that would be a sign-in that
    # fails for no reason the person can see - but an objectionable one is not
    # shown either.
    name = claims["name"] or claims["email"].split("@")[0]
    if moderation.objectionable(name):
        name = "Gimik member"
    user = User(
        email=claims["email"],
        # No password: this account has never had one. See User.password_hash.
        password_hash=None,
        google_sub=claims["sub"],
        name=name[:80],
        handle=await _unique_handle(session, name),
        email_verified=True,
    )
    session.add(user)
    await session.commit()
    # No verification mail: the address arrived already proved. Welcome only.
    email_service.send_welcome(tasks, to=user.email, name=user.name)
    return _pair_for(user)


@router.post(
    "/apple",
    response_model=TokenPair,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def apple_sign_in(
    payload: AppleSignInIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> TokenPair:
    """Sign in - or sign up - with an Apple identity token.

    The same three cases as Google, in the same order, and linking by email is
    safe for the same reason. A "Hide My Email" relay address simply never
    matches an existing account, so those always land as new or returning.
    """
    try:
        claims = await run_in_threadpool(apple_auth.verify_identity_token, payload.identity_token)
    except apple_auth.AppleAuthError as exc:
        log.info("Apple sign-in rejected: %s", exc)
        raise HTTPException(
            status_code=401, detail="That Apple sign-in could not be verified"
        ) from exc

    refresh_token = (
        await apple_auth.exchange_code(payload.authorization_code)
        if payload.authorization_code
        else None
    )

    user = await session.scalar(select(User).where(User.apple_sub == claims["sub"]))
    if user is None:
        user = await session.scalar(select(User).where(User.email == claims["email"]))
        if user is not None:
            user.apple_sub = claims["sub"]
            user.email_verified = True
    if user is not None:
        if refresh_token:
            user.apple_refresh_token = refresh_token
        await session.commit()
        return _pair_for(user)

    name = (payload.name or "").strip() or claims["email"].split("@")[0]
    if moderation.objectionable(name):
        name = "Gimik member"
    user = User(
        email=claims["email"],
        password_hash=None,
        apple_sub=claims["sub"],
        apple_refresh_token=refresh_token,
        name=name[:80],
        handle=await _unique_handle(session, name),
        email_verified=True,
    )
    session.add(user)
    await session.commit()
    email_service.send_welcome(tasks, to=user.email, name=user.name)
    return _pair_for(user)


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
    # A refresh token is a thirty-day bearer. If the account has signed out or
    # changed its password since this one was minted, it is spent.
    if not token_is_current(claims, user.token_version):
        raise CREDENTIALS_ERROR
    return _pair_for(user)


@router.get("/me", response_model=ProfileOut)
async def me(user: User = Depends(current_user)) -> ProfileOut:
    return _profile(user, include_email=True)


@router.patch("/me", response_model=ProfileOut)
async def update_me(
    payload: ProfilePatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    # Everything on a profile that a stranger can read.
    moderation.screen(
        payload.city, payload.bio, payload.work, payload.education, payload.hometown
    )
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
            tasks,
            to=user.email,
            name=user.name,
            token=create_token(str(user.id), "reset", user.token_version),
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

    # The claim is checked against the row before anything is written: a link
    # minted before the last password change is stale, which is what makes a
    # reset link single-use rather than replayable for its whole lifetime.
    if not token_is_current(claims, user.token_version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset link has already been used. Ask for another.",
        )

    user.password_hash = hash_password(payload.password)
    # Spends this link and signs out every other device in one move. Someone
    # resetting a password is often doing it because a session is not theirs.
    user.token_version += 1
    await session.commit()
    # The useful notification is the one you did not expect.
    email_service.send_password_changed(tasks, to=user.email, name=user.name)
    return _pair_for(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Sign out everywhere, and mean it.

    Dropping the tokens on the device is what the app already did, and it is
    enough right up until the moment the device is not the problem. Bumping the
    version is the half that reaches the copy of the refresh token someone else
    took - the only thing a stateless scheme can do, and the reason the column
    exists.
    """
    user.token_version += 1
    await session.commit()


@router.post("/email/verify", response_model=ProfileOut)
async def verify_email(
    payload: VerifyEmailIn,
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    """Prove the address. Deliberately unauthenticated.

    The link is opened from an inbox, which is very often not the device that
    is signed in - a desktop, a second phone. Requiring a bearer here would
    make the most ordinary path the broken one. The token is the credential,
    and it grants exactly one thing.
    """
    try:
        claims = decode_token(payload.token, expect="verify")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That link has expired. Ask for another from Settings.",
        ) from error

    user = await session.scalar(
        select(User).options(selectinload(User.photos)).where(User.id == user_id)
    )
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That link is no longer valid")

    # Idempotent on purpose: mail clients prefetch links, and the second visit
    # is the one the person actually made. It must not read as a failure.
    if not user.email_verified:
        user.email_verified = True
        await session.commit()
    return _profile(user, include_email=True)


@router.post(
    "/email/resend",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SIGN_IN))],
)
async def resend_verification(
    payload: EmailIn,
    tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Always 204, for the same reason /password/forgot always is: the response
    must not sort a list of addresses into those with accounts and those without."""
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is not None and not user.email_verified:
        email_service.send_verify_email(
            tasks,
            to=user.email,
            name=user.name,
            token=create_token(str(user.id), "verify", user.token_version),
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
    apple_token = user.apple_refresh_token
    await session.delete(user)
    await session.commit()
    # App Store rules: deleting an account that used Sign in with Apple must
    # revoke its Apple tokens. A failure is logged, never a reason to keep data.
    if apple_token:
        try:
            await apple_auth.revoke(apple_token)
        except Exception:  # noqa: BLE001
            log.warning("could not revoke Apple token for deleted user %s", user.id)
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception:  # noqa: BLE001 - a stranded object must not fail the deletion
            log.warning("could not remove %s for deleted user %s", key, user.id)
