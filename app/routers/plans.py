"""Lock -> plan -> done -> journal -> record. Always the same steps in the same
order, exactly as the corpus draws the journey."""

from __future__ import annotations

import logging
import uuid

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import current_user
from app.models import (
    SPLITS,
    TIME_SLOTS,
    JournalEntry,
    Keep,
    Place,
    Plan,
    PlanPerson,
    Someday,
    Thread,
    ThreadParticipant,
    User,
)
from app.models.base import utcnow
from app.routers import places
from app.routers.common import holder_out, someday_out, users_by_ids
from app.schemas import JournalIn, JournalOut, PlanCreate, PlanOut, PlanPatch
from app.services import images, storage

router = APIRouter(tags=["plans"])
log = logging.getLogger("gimik.plans")


async def _plan_out(session: AsyncSession, plan: Plan) -> PlanOut:
    someday = await session.get(Someday, plan.someday_id)
    assert someday is not None
    people = await users_by_ids(session, {p.user_id for p in plan.people})
    thread_id = await session.scalar(select(Thread.id).where(Thread.plan_id == plan.id))
    return PlanOut(
        id=plan.id,
        someday=someday_out(someday),
        date=plan.date,
        time_slot=plan.time_slot,
        place=plan.place,
        place_id=plan.place_id,
        split=plan.split,
        people=[holder_out(person) for person in people.values()],
        confirmed_ids=[p.user_id for p in plan.people if p.confirmed],
        done_at=plan.done_at,
        thread_id=thread_id,
        created_at=plan.created_at,
    )


async def _owned(session: AsyncSession, plan_id: uuid.UUID, user_id: uuid.UUID) -> Plan:
    plan = await session.scalar(
        select(Plan).options(selectinload(Plan.people)).where(Plan.id == plan_id)
    )
    if plan is None or plan.owner_id != user_id:
        raise HTTPException(status_code=404, detail="No such plan")
    return plan


def _check(payload: PlanCreate | PlanPatch) -> None:
    if payload.time_slot is not None and payload.time_slot not in TIME_SLOTS:
        raise HTTPException(status_code=422, detail="Unknown time slot")
    if getattr(payload, "split", None) is not None and payload.split not in SPLITS:
        raise HTTPException(status_code=422, detail="The bill is KKB, Equal Split, My Treat, or Others")


@router.get("/plans", response_model=list[PlanOut])
async def my_plans(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PlanOut]:
    plans = (
        await session.scalars(
            select(Plan)
            .options(selectinload(Plan.people))
            .where(Plan.owner_id == user.id)
            .order_by(Plan.date)
        )
    ).all()
    return [await _plan_out(session, plan) for plan in plans]


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def lock(
    payload: PlanCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    _check(payload)
    someday = await session.get(Someday, payload.someday_id)
    if someday is None:
        raise HTTPException(status_code=404, detail="No such someday")
    existing = await session.scalar(
        select(Plan).where(
            Plan.owner_id == user.id,
            Plan.someday_id == payload.someday_id,
            Plan.done_at.is_(None),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Already locked")

    # Locking implies keeping: the someday joins the list if it was not on it.
    if await session.get(Keep, (user.id, payload.someday_id)) is None:
        session.add(Keep(user_id=user.id, someday_id=payload.someday_id))

    plan = Plan(
        someday_id=payload.someday_id,
        owner_id=user.id,
        date=payload.date,
        time_slot=payload.time_slot,
        place=payload.place,
        place_id=payload.place_id,
        split=payload.split,
    )
    session.add(plan)
    await session.flush()
    # A place becomes worth locating the moment somebody commits to going. One
    # Details call, cached on the row - see places.ensure_coordinates for why it
    # does not happen while searching.
    if plan.place_id is not None:
        chosen = await session.get(Place, plan.place_id)
        if chosen is not None:
            await places.ensure_coordinates(session, chosen)
    member_ids = {user.id, *payload.people}
    for member_id in member_ids:
        if member_id != user.id and await session.get(User, member_id) is None:
            raise HTTPException(status_code=404, detail="No such person on the plan")
        session.add(
            PlanPerson(plan_id=plan.id, user_id=member_id, confirmed=member_id == user.id)
        )
    # The plan thread exists from the moment the plan does (corpus 5).
    thread = Thread(kind="plan", plan_id=plan.id, someday_id=plan.someday_id)
    session.add(thread)
    await session.flush()
    for member_id in member_ids:
        session.add(ThreadParticipant(thread_id=thread.id, user_id=member_id))
    await session.commit()

    loaded = await session.scalar(
        select(Plan).options(selectinload(Plan.people)).where(Plan.id == plan.id)
    )
    assert loaded is not None
    return await _plan_out(session, loaded)


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID,
    payload: PlanPatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    _check(payload)
    plan = await _owned(session, plan_id, user.id)
    if payload.date is not None:
        plan.date = payload.date
    if payload.time_slot is not None:
        plan.time_slot = payload.time_slot
    if payload.place is not None:
        plan.place = payload.place
    if payload.place_id is not None:
        plan.place_id = payload.place_id
    if payload.split is not None:
        plan.split = payload.split
    if payload.people is not None:
        wanted = {user.id, *payload.people}
        current = {p.user_id: p for p in plan.people}
        for gone in set(current) - wanted:
            await session.delete(current[gone])
        for added in wanted - set(current):
            if added != user.id and await session.get(User, added) is None:
                raise HTTPException(status_code=404, detail="No such person on the plan")
            session.add(PlanPerson(plan_id=plan.id, user_id=added, confirmed=added == user.id))
        thread = await session.scalar(select(Thread).where(Thread.plan_id == plan.id))
        if thread is not None:
            for added in wanted:
                if await session.get(ThreadParticipant, (thread.id, added)) is None:
                    session.add(ThreadParticipant(thread_id=thread.id, user_id=added))
    await session.commit()
    refreshed = await _owned(session, plan_id, user.id)
    return await _plan_out(session, refreshed)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlock(
    plan_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Back to waiting for a date. The keep stays; only the date goes."""
    plan = await _owned(session, plan_id, user.id)
    await session.delete(plan)
    await session.commit()


@router.post("/plans/{plan_id}/done", response_model=PlanOut)
async def mark_done(
    plan_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = await _owned(session, plan_id, user.id)
    if plan.done_at is None:
        plan.done_at = utcnow()
        await session.commit()
    return await _plan_out(session, plan)


# --------------------------------------------------------------------- journal


@router.get("/somedays/{someday_id}/journal", response_model=list[JournalOut])
async def journal(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[JournalOut]:
    rows = (
        await session.scalars(
            select(JournalEntry)
            .where(JournalEntry.user_id == user.id, JournalEntry.someday_id == someday_id)
            .order_by(JournalEntry.created_at.desc())
        )
    ).all()
    return [
        JournalOut(
            id=row.id,
            someday_id=row.someday_id,
            body=row.body,
            photo_url=storage.presign_download(row.photo_key) if row.photo_key else None,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post(
    "/somedays/{someday_id}/journal",
    response_model=JournalOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_journal(
    someday_id: uuid.UUID,
    payload: JournalIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JournalOut:
    if not payload.body.strip() and not payload.photo_key:
        raise HTTPException(status_code=422, detail="An entry needs words or a photo")
    if await session.get(Someday, someday_id) is None:
        raise HTTPException(status_code=404, detail="No such someday")
    photo_key = None
    if payload.photo_key:
        if not payload.photo_key.startswith((f"users/{user.id}/", f"{user.id}/")):
            raise HTTPException(status_code=403, detail="Not your object")
        try:
            storage.upload_key_owner(payload.photo_key)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            photo_key = await anyio.to_thread.run_sync(images.process_upload, payload.photo_key, limiter=images.image_limiter)
        except images.InvalidImage as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (BotoCoreError, ClientError) as error:
            raise HTTPException(status_code=503, detail="Photo storage is temporarily unavailable. Try again.") from error
    entry = JournalEntry(
        user_id=user.id,
        someday_id=someday_id,
        body=payload.body.strip(),
        photo_key=photo_key,
    )
    session.add(entry)
    await session.commit()
    if payload.photo_key:
        try:
            await anyio.to_thread.run_sync(storage.delete_object, payload.photo_key)
        except (BotoCoreError, ClientError):
            log.warning("could not remove original journal photo after normalization")
    return JournalOut(
        id=entry.id,
        someday_id=entry.someday_id,
        body=entry.body,
        photo_url=storage.presign_download(entry.photo_key) if entry.photo_key else None,
        created_at=entry.created_at,
    )
