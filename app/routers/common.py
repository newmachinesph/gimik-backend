"""Serializers and the small relationship queries every router needs."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Friendship, GroupMember, HiddenUser, Someday, User
from app.schemas import HolderOut, SomedayOut
from app.services import storage


def someday_out(someday: Someday) -> SomedayOut:
    """Catalog artwork rides along as a plain URL into the public bucket the v1
    stack seeded; custom somedays have no slug and therefore no artwork."""
    out = SomedayOut.model_validate(someday)
    if someday.slug:
        out.artwork_url = storage.catalog_url(someday.slug)
    return out


def holder_out(user: User) -> HolderOut:
    return HolderOut(
        id=user.id, name=user.name, handle=user.handle, gender=user.gender, city=user.city
    )


async def friend_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Accepted friendships in either direction; mutual by construction."""
    rows = (
        await session.execute(
            select(Friendship.requester_id, Friendship.addressee_id).where(
                Friendship.status == "accepted",
                or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id),
            )
        )
    ).all()
    ids: set[uuid.UUID] = set()
    for requester, addressee in rows:
        ids.add(addressee if requester == user_id else requester)
    return ids


async def group_peer_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Everyone sharing at least one group with this person.

    One query for the whole set, rather than one per candidate. The holders list
    used to ask "do we share a group?" inside its loop, which is a query per
    holder and turns a popular someday into a few hundred sequential round trips
    to answer a question whose answer never changes during the request.
    """
    my_groups = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    rows = await session.scalars(
        select(GroupMember.user_id).where(
            GroupMember.group_id.in_(my_groups), GroupMember.user_id != user_id
        )
    )
    return set(rows.all())


async def hidden_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (
        await session.scalars(
            select(HiddenUser.hidden_user_id).where(HiddenUser.user_id == user_id)
        )
    ).all()
    return set(rows)


async def blocked_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Everyone this person blocked, and everyone who blocked them.

    A block cuts both ways: neither side sees the other's somedays, profile or
    messages, and neither can reach the other. `hidden_ids` is the one-way half,
    for the list a person manages themselves.
    """
    rows = (
        await session.execute(
            select(HiddenUser.user_id, HiddenUser.hidden_user_id).where(
                or_(HiddenUser.user_id == user_id, HiddenUser.hidden_user_id == user_id)
            )
        )
    ).all()
    return {theirs if mine == user_id else mine for mine, theirs in rows}


async def users_by_ids(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not ids:
        return {}
    rows = (await session.scalars(select(User).where(User.id.in_(ids)))).all()
    return {user.id: user for user in rows}
