"""Private media: presigned upload, profile photos (up to eight, first is the
face everywhere). Catalog artwork never comes through here - it is public by
design and served straight from the catalog bucket."""

from __future__ import annotations

import logging
import uuid

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.entitlements import PLUS_PHOTOS, PlusRequired, photo_allowance
from app.models import User, UserPhoto
from app.schemas import ClaimPhotoIn, PhotoOut, PresignUploadIn, PresignUploadOut
from app.services import images, ratelimit, storage

router = APIRouter(prefix="/media", tags=["media"])
log = logging.getLogger("gimik.media")

# The ceiling for anyone; the tier decides how much of it an account gets.
MAX_PHOTOS = PLUS_PHOTOS
ALLOWED_TYPES = images.ALLOWED_TYPES
# Bound decoder memory and keep blocking S3/codec work off the event loop.


@router.post(
    "/presign",
    response_model=PresignUploadOut,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SOCIAL_WRITE))],
)
async def presign(
    payload: PresignUploadIn,
    user: User = Depends(current_user),
) -> PresignUploadOut:
    if payload.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=422, detail="Images only")
    key = storage.object_key(str(user.id), payload.filename)
    return PresignUploadOut(key=key, upload_url=storage.presign_upload(key, payload.content_type))


@router.post("/photos", response_model=PhotoOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(ratelimit.limiter(ratelimit.SOCIAL_WRITE))])
async def add_photo(
    payload: ClaimPhotoIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PhotoOut:
    # Only keys inside the caller's own namespace can become their photos.
    if not payload.key.startswith((f"users/{user.id}/", f"{user.id}/")):
        raise HTTPException(status_code=403, detail="Not your object")
    try:
        storage.upload_key_owner(payload.key)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    # Serialize claims for this account so parallel uploads cannot reuse a slot.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = (
        await session.scalars(
            select(UserPhoto).where(UserPhoto.user_id == user.id).order_by(UserPhoto.position)
        )
    ).all()
    target = images.normalized_key(payload.key)
    claimed = next((photo for photo in existing if photo.object_key in {target, target.removeprefix("users/")}), None)
    if claimed:
        return PhotoOut(id=claimed.id, position=claimed.position, url=storage.presign_download(claimed.object_key))
    allowance = photo_allowance(user)
    if len(existing) >= allowance:
        # 402 when more is purchasable, 409 when it simply is not - the app
        # opens the paywall on the first and shows a plain limit on the second.
        if not user.is_plus:
            raise PlusRequired(f"A {PLUS_PHOTOS}-photo profile")
        raise HTTPException(status_code=409, detail=f"{allowance} photos is the limit")
    try:
        target = await anyio.to_thread.run_sync(images.process_upload, payload.key, limiter=images.image_limiter)
    except images.InvalidImage as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (BotoCoreError, ClientError) as error:
        log.exception("profile image storage failed")
        raise HTTPException(status_code=503, detail="Photo storage is temporarily unavailable. Try again.") from error
    photo = UserPhoto(
        user_id=user.id,
        object_key=target,
        position=(existing[-1].position + 1) if existing else 0,
    )
    session.add(photo)
    await session.commit()
    try:
        await anyio.to_thread.run_sync(storage.delete_object, payload.key)
    except (BotoCoreError, ClientError):
        log.warning("could not remove original photo after normalization")
    return PhotoOut(id=photo.id, position=photo.position, url=storage.presign_download(photo.object_key))


@router.delete("/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_photo(
    photo_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    photo = await session.get(UserPhoto, photo_id)
    if photo is None or photo.user_id != user.id:
        raise HTTPException(status_code=404, detail="No such photo")
    key = photo.object_key
    await session.delete(photo)
    await session.commit()
    try:
        storage.delete_object(key)
    except Exception:  # noqa: BLE001 - a stranded object must not fail the delete
        log.warning("could not remove %s from the store", key)


@router.post("/photos/{photo_id}/first", status_code=status.HTTP_204_NO_CONTENT)
async def make_first(
    photo_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Make it your face: position 0, everything else shuffles up."""
    photos = (
        await session.scalars(
            select(UserPhoto).where(UserPhoto.user_id == user.id).order_by(UserPhoto.position)
        )
    ).all()
    chosen = next((photo for photo in photos if photo.id == photo_id), None)
    if chosen is None:
        raise HTTPException(status_code=404, detail="No such photo")
    ordered = [chosen] + [photo for photo in photos if photo.id != photo_id]
    # Two passes so the unique (user_id, position) constraint never collides.
    for index, photo in enumerate(ordered):
        photo.position = 1000 + index
    await session.flush()
    for index, photo in enumerate(ordered):
        photo.position = index
    await session.commit()
