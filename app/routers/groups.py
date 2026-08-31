"""Groups: name it, share the link, whoever opens it is in. Every group has one
thread; leaving the group leaves the thread."""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import current_user
from app.entitlements import requires_plus
from app.models import Group, GroupMember, Thread, ThreadParticipant, User
from app.routers.common import holder_out, users_by_ids
from app.schemas import GroupCreate, GroupOut

router = APIRouter(prefix="/groups", tags=["groups"])


async def _group_out(session: AsyncSession, group: Group) -> GroupOut:
    member_ids = {member.user_id for member in group.members}
    people = await users_by_ids(session, member_ids)
    return GroupOut(
        id=group.id,
        name=group.name,
        invite_token=group.invite_token,
        members=[holder_out(people[uid]) for uid in member_ids if uid in people],
        created_at=group.created_at,
    )


async def _load(session: AsyncSession, group_id: uuid.UUID) -> Group:
    group = await session.scalar(
        select(Group)
        .options(selectinload(Group.members))
        # The group may already sit in the identity map with a members list
        # loaded before this request's insert; refresh it rather than trust it.
        .execution_options(populate_existing=True)
        .where(Group.id == group_id)
    )
    if group is None:
        raise HTTPException(status_code=404, detail="No such group")
    return group


async def _group_thread(session: AsyncSession, group: Group) -> Thread:
    thread = await session.scalar(select(Thread).where(Thread.group_id == group.id))
    if thread is None:
        thread = Thread(kind="group", group_id=group.id)
        session.add(thread)
        await session.flush()
    return thread


@router.get("", response_model=list[GroupOut])
async def my_groups(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[GroupOut]:
    groups = (
        await session.scalars(
            select(Group)
            .join(GroupMember)
            .options(selectinload(Group.members))
            .where(GroupMember.user_id == user.id)
        )
    ).all()
    return [await _group_out(session, group) for group in groups]


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    # Creating a group opens an Ably channel that lives as long as the group.
    # Joining one does not, and is never gated - a group nobody free can enter
    # is not a group.
    user: User = Depends(requires_plus("Creating groups")),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = Group(
        name=payload.name.strip(),
        invite_token=secrets.token_urlsafe(12),
        created_by=user.id,
    )
    session.add(group)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=user.id))
    thread = Thread(kind="group", group_id=group.id)
    session.add(thread)
    await session.flush()
    session.add(ThreadParticipant(thread_id=thread.id, user_id=user.id))
    await session.commit()
    loaded = await _load(session, group.id)
    return await _group_out(session, loaded)


@router.get("/{group_id}", response_model=GroupOut)
async def group_detail(
    group_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = await _load(session, group_id)
    if user.id not in {member.user_id for member in group.members}:
        raise HTTPException(status_code=404, detail="No such group")
    return await _group_out(session, group)


@router.post("/join/{token}", response_model=GroupOut)
async def join_group(
    token: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = await session.scalar(
        select(Group).options(selectinload(Group.members)).where(Group.invite_token == token)
    )
    if group is None:
        raise HTTPException(status_code=404, detail="This link has expired")
    if await session.get(GroupMember, (group.id, user.id)) is None:
        session.add(GroupMember(group_id=group.id, user_id=user.id))
        thread = await _group_thread(session, group)
        if await session.get(ThreadParticipant, (thread.id, user.id)) is None:
            session.add(ThreadParticipant(thread_id=thread.id, user_id=user.id))
        await session.commit()
    group = await _load(session, group.id)
    return await _group_out(session, group)


@router.delete("/{group_id}/membership", status_code=status.HTTP_204_NO_CONTENT)
async def leave_group(
    group_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    membership = await session.get(GroupMember, (group_id, user.id))
    if membership is None:
        return
    await session.delete(membership)
    thread = await session.scalar(select(Thread).where(Thread.group_id == group_id))
    if thread is not None:
        participant = await session.get(ThreadParticipant, (thread.id, user.id))
        if participant is not None:
            await session.delete(participant)
    await session.commit()
