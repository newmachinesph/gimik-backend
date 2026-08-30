"""The catalog, and what a person has done with it: keep, pass, visibility.

Every keep carries the two visibility axes asked on the card. Who is allowed to
see a keep - and therefore invite its holder - is decided here and only here:
friends when open to Friends, shared group members when open to Groups, same
city when open to Everyone.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import AUDIENCES, INVITE_PARTIES, GroupMember, Keep, Pass, Someday, User
from app.routers.common import friend_ids, holder_out, someday_out, users_by_ids
from app.schemas import HolderOut, KeepIn, KeepOut, SomedayOut, Visibility

router = APIRouter(tags=["catalog"])


async def _someday(session: AsyncSession, someday_id: uuid.UUID) -> Someday:
    someday = await session.get(Someday, someday_id)
    if someday is None:
        raise HTTPException(status_code=404, detail="No such someday")
    return someday


def _check_visibility(visibility: Visibility) -> None:
    if not set(visibility.open_to) <= AUDIENCES:
        raise HTTPException(status_code=422, detail="Unknown audience")
    if not set(visibility.party_as) <= INVITE_PARTIES:
        raise HTTPException(status_code=422, detail="Unknown party shape")


@router.get("/catalog", response_model=list[SomedayOut])
async def catalog(
    q: str | None = None,
    category: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[SomedayOut]:
    """The whole seeded catalog. Custom somedays are private to their owner and
    are not listed here."""
    stmt = select(Someday).where(Someday.slug.is_not(None)).order_by(Someday.title)
    if category:
        stmt = stmt.where(Someday.category == category)
    if q:
        stmt = stmt.where(Someday.title.ilike(f"%{q.strip()}%"))
    rows = (await session.scalars(stmt)).all()
    return [someday_out(row) for row in rows]


@router.get("/me/somedays", response_model=list[KeepOut])
async def my_somedays(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[KeepOut]:
    rows = (
        await session.execute(
            select(Keep, Someday)
            .join(Someday, Someday.id == Keep.someday_id)
            .where(Keep.user_id == user.id)
            .order_by(Keep.created_at)
        )
    ).all()
    return [
        KeepOut(
            someday=someday_out(someday),
            open_to=keep.open_to,
            party_as=keep.party_as,
            kept_at=keep.created_at,
        )
        for keep, someday in rows
    ]


@router.put("/somedays/{someday_id}/keep", response_model=KeepOut, status_code=status.HTTP_201_CREATED)
async def keep(
    someday_id: uuid.UUID,
    payload: KeepIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KeepOut:
    _check_visibility(payload)
    someday = await _someday(session, someday_id)
    existing = await session.get(Keep, (user.id, someday_id))
    if existing is None:
        existing = Keep(user_id=user.id, someday_id=someday_id)
        session.add(existing)
    existing.open_to = payload.open_to
    existing.party_as = payload.party_as
    # A keep un-passes: the two are exclusive answers to the same card.
    passed = await session.get(Pass, (user.id, someday_id))
    if passed is not None:
        await session.delete(passed)
    await session.commit()
    return KeepOut(
        someday=someday_out(someday),
        open_to=existing.open_to,
        party_as=existing.party_as,
        kept_at=existing.created_at,
    )


@router.delete("/somedays/{someday_id}/keep", status_code=status.HTTP_204_NO_CONTENT)
async def unkeep(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    existing = await session.get(Keep, (user.id, someday_id))
    if existing is not None:
        await session.delete(existing)
        await session.commit()


@router.put("/somedays/{someday_id}/pass", status_code=status.HTTP_204_NO_CONTENT)
async def pass_someday(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _someday(session, someday_id)
    if await session.get(Pass, (user.id, someday_id)) is None:
        session.add(Pass(user_id=user.id, someday_id=someday_id))
    kept = await session.get(Keep, (user.id, someday_id))
    if kept is not None:
        await session.delete(kept)
    await session.commit()


@router.delete("/somedays/{someday_id}/pass", status_code=status.HTTP_204_NO_CONTENT)
async def unpass(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    existing = await session.get(Pass, (user.id, someday_id))
    if existing is not None:
        await session.delete(existing)
        await session.commit()


@router.put("/somedays/{someday_id}/visibility", response_model=KeepOut)
async def set_visibility(
    someday_id: uuid.UUID,
    payload: Visibility,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KeepOut:
    _check_visibility(payload)
    someday = await _someday(session, someday_id)
    existing = await session.get(Keep, (user.id, someday_id))
    if existing is None:
        raise HTTPException(status_code=404, detail="Not on your list")
    existing.open_to = payload.open_to
    existing.party_as = payload.party_as
    await session.commit()
    return KeepOut(
        someday=someday_out(someday),
        open_to=existing.open_to,
        party_as=existing.party_as,
        kept_at=existing.created_at,
    )


@router.get("/somedays/{someday_id}/holders", response_model=list[HolderOut])
async def holders(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[HolderOut]:
    """Who's in: the people whose keep of this someday is visible to the caller.

    Visibility is the holder's choice, per someday, on their keep: Friends lets
    accepted friends see it, Groups lets shared-group members see it, Everyone
    lets anyone in the same city see it. An empty open_to is Just me.
    """
    await _someday(session, someday_id)
    keeps = (
        await session.scalars(
            select(Keep).where(Keep.someday_id == someday_id, Keep.user_id != user.id)
        )
    ).all()
    if not keeps:
        return []

    my_friends = await friend_ids(session, user.id)
    my_groups = set(
        (
            await session.scalars(
                select(GroupMember.group_id).where(GroupMember.user_id == user.id)
            )
        ).all()
    )
    holders_users = await users_by_ids(session, {keep.user_id for keep in keeps})

    visible: list[HolderOut] = []
    for keep_row in keeps:
        holder = holders_users.get(keep_row.user_id)
        if holder is None:
            continue
        open_to = set(keep_row.open_to)
        allowed = False
        if "friends" in open_to and holder.id in my_friends:
            allowed = True
        if not allowed and "groups" in open_to and my_groups:
            shared = await session.scalar(
                select(GroupMember.group_id)
                .where(GroupMember.user_id == holder.id, GroupMember.group_id.in_(my_groups))
                .limit(1)
            )
            allowed = shared is not None
        if not allowed and "everyone" in open_to and holder.city and holder.city == user.city:
            allowed = True
        if allowed:
            visible.append(holder_out(holder))
    return visible
