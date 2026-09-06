"""The catalog, and what a person has done with it: keep, pass, visibility.

Every keep carries the two visibility axes asked on the card. Who is allowed to
see a keep - and therefore invite its holder - is decided here and only here:
friends when open to Friends, shared group members when open to Groups, same
city when open to Everyone.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import (
    AUDIENCES,
    INVITE_PARTIES,
    Keep,
    Pass,
    Someday,
    SomedayProgress,
    User,
)
from app.routers.common import (
    friend_ids,
    group_peer_ids,
    holder_out,
    someday_out,
    users_by_ids,
)
from app.schemas import (
    CatalogPage,
    HolderOut,
    KeepIn,
    KeepOut,
    ProgressIn,
    ProgressOut,
    SomedayCreate,
    Visibility,
)
from app.services import ratelimit
from app.taxonomy import CATEGORIES, canonical, is_canonical

router = APIRouter(tags=["catalog"])

# Two screens of a two-column grid, so the first scroll never waits.
PAGE_SIZE = 40
MAX_PAGE_SIZE = 100


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


@router.get("/categories", response_model=list[str])
async def categories() -> list[str]:
    """The filter chips, in the order the app shows them.

    Served rather than hard-coded in the client so the two cannot drift: a
    category the catalog no longer has is a chip that returns nothing.
    """
    return list(CATEGORIES)


@router.get("/catalog", response_model=CatalogPage)
async def catalog(
    q: str | None = None,
    category: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> CatalogPage:
    """One page of the seeded catalog. Custom somedays are private to their
    owner and are not listed here.

    This used to return everything. At 522 concepts that was merely wasteful; at
    2,522 it is a multi-megabyte response the grid throws away all but two
    screens of, so it pages. Ordering stays alphabetical: this is the browse
    surface, where people are looking for a thing they already have in mind and
    a stable A-Z is easier to scan than a ranking that moves. The deck is where
    ranking belongs, and that is `GET /feed`.
    """
    filters = [Someday.slug.is_not(None)]
    if category:
        # Accept the older one-word names so a client mid-update still filters.
        filters.append(Someday.category == canonical(category))
    if q:
        filters.append(Someday.title.ilike(f"%{q.strip()}%"))

    total = await session.scalar(select(func.count()).select_from(Someday).where(*filters)) or 0
    rows = (
        await session.scalars(
            select(Someday).where(*filters).order_by(Someday.title).offset(offset).limit(limit)
        )
    ).all()
    served = offset + len(rows)
    return CatalogPage(
        items=[someday_out(row) for row in rows],
        total=total,
        next_offset=served if served < total else None,
    )


@router.post(
    "/somedays",
    response_model=KeepOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SOCIAL_WRITE))],
)
async def create_someday(
    payload: SomedayCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KeepOut:
    """Write your own someday. It is yours, kept, and visible on your terms.

    The catalog is 2,522 concepts and still will not contain "drive to my lola's
    town and find the bakery she talks about". Until now there was nowhere to put
    a sentence like that: the column for it existed and the feed already knew how
    to show other people's, but nothing could create one, so that pool was always
    empty.

    Three things happen at once, and they are one act rather than three:

    - The row is written with `owner_id` set and no slug, which is what makes it
      custom - no slug means no catalog artwork, because nobody drew one.
    - It is kept immediately. Writing a someday down is wanting it; making the
      author swipe on their own sentence would be theatre.
    - The keep carries the visibility from the same form, which is what decides
      whether anyone else ever sees it. `everyone` reaches the same city, and
      `friends` reaches friends - the identical rule the rest of the app uses.

    Creating the same sentence twice returns the first one rather than a second
    row. People retype things, and two identical somedays would split their own
    holders between them.
    """
    _check_visibility(payload)
    category = canonical(payload.category)
    if not is_canonical(category):
        raise HTTPException(status_code=422, detail="Unknown category")

    title = payload.title.strip()
    existing = await session.scalar(
        select(Someday).where(
            Someday.owner_id == user.id,
            Someday.slug.is_(None),
            func.lower(Someday.title) == title.lower(),
        )
    )
    someday = existing
    if someday is None:
        someday = Someday(
            title=title,
            category=category,
            owner_id=user.id,
            party="any",
            register="custom",
            needs=[],
        )
        session.add(someday)
        await session.flush()

    keep_row = await session.get(Keep, (user.id, someday.id))
    if keep_row is None:
        keep_row = Keep(user_id=user.id, someday_id=someday.id)
        session.add(keep_row)
    keep_row.open_to = payload.open_to
    keep_row.party_as = payload.party_as
    await session.commit()
    return KeepOut(
        someday=someday_out(someday),
        open_to=keep_row.open_to,
        party_as=keep_row.party_as,
        kept_at=keep_row.created_at,
    )


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
    # One extra query for the whole list rather than one per row: most keeps are
    # ordinary and carry no progress at all.
    trackable = [someday.id for _, someday in rows if someday.completion != "single"]
    progress: dict[uuid.UUID, SomedayProgress] = {}
    if trackable:
        progress = {
            row.someday_id: row
            for row in (
                await session.scalars(
                    select(SomedayProgress).where(
                        SomedayProgress.user_id == user.id,
                        SomedayProgress.someday_id.in_(trackable),
                    )
                )
            ).all()
        }
    return [
        KeepOut(
            someday=someday_out(someday),
            open_to=keep.open_to,
            party_as=keep.party_as,
            kept_at=keep.created_at,
            progress=(
                _progress_out(progress[someday.id], someday)
                if someday.id in progress
                else None
            ),
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


def _progress_out(row: SomedayProgress, someday: Someday) -> ProgressOut:
    target = row.target if row.target is not None else someday.target
    return ProgressOut(
        someday_id=someday.id,
        count=row.count,
        target=target,
        last_at=row.last_at,
        complete=target is not None and row.count >= target,
    )


@router.post("/somedays/{someday_id}/progress", response_model=ProgressOut)
async def add_progress(
    someday_id: uuid.UUID,
    payload: ProgressIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProgressOut:
    """Chip away at something that cannot be done in one evening.

    The catalog has carried a `completion` of count, distance, duration, streak
    or percentage since it was written, and until now nothing could record a
    single island of "100 islands in a year" - the app offered a Lock-a-date
    button and called it finished. This is the missing verb.

    Refused on a `single` someday rather than quietly accepted: a count against
    a thing that happens once means the caller has misunderstood the concept,
    and silently storing it would leave a number nothing knows how to show.

    `target` is copied from the catalog the first time somebody starts, so
    editing the catalog later cannot rewrite what a person was working toward.
    """
    someday = await _someday(session, someday_id)
    if someday.completion == "single":
        raise HTTPException(status_code=409, detail="This one happens once")

    row = await session.get(SomedayProgress, (user.id, someday_id))
    if row is None:
        # count=0 explicitly: the column default is applied by the database at
        # INSERT, so a freshly constructed row still reads None in Python and
        # the first increment would be None + 1.
        row = SomedayProgress(
            user_id=user.id, someday_id=someday_id, target=someday.target, count=0
        )
        session.add(row)
    # Never below zero: an undo past the start is a correction, not a debt.
    row.count = max(0, row.count + payload.step)
    row.last_at = dt.datetime.now(dt.UTC)
    await session.commit()
    return _progress_out(row, someday)


@router.get("/somedays/{someday_id}/progress", response_model=ProgressOut)
async def read_progress(
    someday_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProgressOut:
    someday = await _someday(session, someday_id)
    row = await session.get(SomedayProgress, (user.id, someday_id))
    if row is None:
        return ProgressOut(
            someday_id=someday_id, count=0, target=someday.target, last_at=None, complete=False
        )
    return _progress_out(row, someday)


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
    my_peers = await group_peer_ids(session, user.id)
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
        if not allowed and "groups" in open_to:
            allowed = holder.id in my_peers
        if not allowed and "everyone" in open_to and holder.city and holder.city == user.city:
            allowed = True
        if allowed:
            visible.append(holder_out(holder))
    return visible
