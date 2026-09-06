"""People reaching each other: invites, friendships, hiding, reporting, and the
public face of a person.

The quiet rules from the corpus hold everywhere here: declining is silent,
hiding is silent and reversible, a report is never shown to its subject, and
nobody is a friend until both sides said so.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.db import get_session
from app.deps import current_user
from app.entitlements import FREE_INVITES_PER_WEEK, PlusRequired
from app.models import (
    INVITE_PARTIES,
    Friendship,
    GroupMember,
    HiddenUser,
    Invite,
    Keep,
    Report,
    Someday,
    Thread,
    ThreadParticipant,
    User,
)
from app.routers.common import friend_ids, hidden_ids, holder_out, someday_out, users_by_ids
from app.schemas import (
    FriendRequestOut,
    FriendsOut,
    HolderOut,
    InviteCreate,
    InviteOut,
    PersonOut,
    PhotoOut,
    ReportIn,
)
from app.services import push, ratelimit, realtime, storage

router = APIRouter(tags=["social"])

# Two characters is enough to be looking for somebody and short enough to be
# useless for enumerating a user base one letter at a time.
SEARCH_FLOOR = 2
SEARCH_LIMIT = 20


# ---------------------------------------------------------------------- invites


async def _open_solo_thread(
    session: AsyncSession, a: uuid.UUID, b: uuid.UUID, someday_id: uuid.UUID | None
) -> Thread:
    """One solo thread per pair, ever; the invite's someday pins to it.

    Asked as a single join on both participants. It used to load every solo
    thread `a` had and then ask, one query each, whether `b` was in it - so
    opening a conversation cost a round trip per conversation that person had
    already had, and got slower the more they used the app.
    """
    mine = aliased(ThreadParticipant)
    theirs = aliased(ThreadParticipant)
    thread = await session.scalar(
        select(Thread)
        .join(mine, mine.thread_id == Thread.id)
        .join(theirs, theirs.thread_id == Thread.id)
        .where(Thread.kind == "solo", mine.user_id == a, theirs.user_id == b)
        .limit(1)
    )
    if thread is not None:
        if someday_id and thread.someday_id is None:
            thread.someday_id = someday_id
        return thread

    thread = Thread(kind="solo", someday_id=someday_id)
    session.add(thread)
    await session.flush()
    session.add_all(
        [
            ThreadParticipant(thread_id=thread.id, user_id=a),
            ThreadParticipant(thread_id=thread.id, user_id=b),
        ]
    )
    return thread


# An invite is a question about a weekend, so it stops being one after a week
# (corpus 7). Enforced as a cutoff on the query rather than by a sweeper: there
# is no job to schedule, no status to migrate, and an invite keeps its row and
# its place in history. The app derives the same boundary from `created_at`, so
# both sides agree without either telling the other.
INVITE_LIFETIME = dt.timedelta(days=7)


@router.get("/invites", response_model=list[InviteOut])
async def invites(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[InviteOut]:
    hidden = await hidden_ids(session, user.id)
    rows = (
        await session.execute(
            select(Invite, Someday)
            .join(Someday, Someday.id == Invite.someday_id)
            .where(
                Invite.to_user_id == user.id,
                Invite.status.in_(["new", "seen"]),
                Invite.created_at > dt.datetime.now(dt.UTC) - INVITE_LIFETIME,
            )
            .order_by(Invite.created_at.desc())
        )
    ).all()
    senders = await users_by_ids(session, {invite.from_user_id for invite, _ in rows})
    out: list[InviteOut] = []
    for invite, someday in rows:
        sender = senders.get(invite.from_user_id)
        if sender is None or sender.id in hidden:
            continue
        out.append(
            InviteOut(
                id=invite.id,
                from_user=holder_out(sender),
                someday=someday_out(someday),
                party=invite.party,
                status=invite.status,
                created_at=invite.created_at,
            )
        )
    return out


@router.post(
    "/invites",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SOCIAL_WRITE))],
)
async def send_invite(
    payload: InviteCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> InviteOut:
    if payload.party not in INVITE_PARTIES:
        raise HTTPException(status_code=422, detail="An invite is duo or group")
    if payload.to_user_id == user.id:
        raise HTTPException(status_code=422, detail="That would be a plan, not an invite")

    # The free ceiling, counted over a rolling week rather than a calendar one
    # so there is no midnight at which everyone's allowance returns at once.
    #
    # Checked before any of the work below: refusing after five queries and a
    # visibility walk costs the database the same as allowing it. Receiving is
    # never limited - only the sending side, which is the half that fans out.
    if not user.is_plus:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
        sent = await session.scalar(
            select(func.count())
            .select_from(Invite)
            .where(Invite.from_user_id == user.id, Invite.created_at >= since)
        )
        if (sent or 0) >= FREE_INVITES_PER_WEEK:
            raise PlusRequired(f"Sending more than {FREE_INVITES_PER_WEEK} invites a week")
    target = await session.get(User, payload.to_user_id)
    someday = await session.get(Someday, payload.someday_id)
    if target is None or someday is None:
        raise HTTPException(status_code=404, detail="No such person or someday")
    # Hidden goes both ways and is never explained (corpus 4).
    if user.id in await hidden_ids(session, target.id) or target.id in await hidden_ids(
        session, user.id
    ):
        raise HTTPException(status_code=404, detail="No such person or someday")
    # The someday must be on the target's list, visibly to the sender: an invite
    # answers a want the sender can actually see.
    target_keep = await session.get(Keep, (target.id, payload.someday_id))
    if target_keep is None:
        raise HTTPException(status_code=409, detail="They do not hold that someday")
    if payload.party not in set(target_keep.party_as):
        raise HTTPException(status_code=409, detail="They are not open to that shape")

    open_to = set(target_keep.open_to)
    allowed = False
    if "friends" in open_to and target.id in await friend_ids(session, user.id):
        allowed = True
    if not allowed and "groups" in open_to:
        shared = await session.scalar(
            select(GroupMember.group_id)
            .where(
                GroupMember.user_id == user.id,
                GroupMember.group_id.in_(
                    select(GroupMember.group_id).where(GroupMember.user_id == target.id)
                ),
            )
            .limit(1)
        )
        allowed = shared is not None
    if not allowed and "everyone" in open_to and target.city and target.city == user.city:
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="That someday is not open to you")

    invite = Invite(
        from_user_id=user.id,
        to_user_id=target.id,
        someday_id=someday.id,
        party=payload.party,
    )
    session.add(invite)
    await session.commit()
    # After the commit, so realtime can never announce something that rolled
    # back. Best effort: a dropped event costs the recipient a refresh.
    await realtime.publish(
        realtime.user_channel(target.id),
        "invite",
        {
            "inviteId": str(invite.id),
            "from": user.name,
            "someday": someday.title,
            "party": invite.party,
            "at": invite.created_at.isoformat(),
        },
    )
    # And to the phone in their pocket. Ably only reaches an app that is open,
    # and an invite nobody sees is an invite that lapses in a week.
    await push.send_to(
        session,
        [target.id],
        title=f"{user.name} wants to do something",
        body=someday.title,
        data={"kind": "invite", "inviteId": str(invite.id)},
    )
    return InviteOut(
        id=invite.id,
        from_user=holder_out(user),
        someday=someday_out(someday),
        party=invite.party,
        status=invite.status,
        created_at=invite.created_at,
    )


@router.post("/invites/{invite_id}/accept")
async def accept_invite(
    invite_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    invite = await session.get(Invite, invite_id)
    if invite is None or invite.to_user_id != user.id:
        raise HTTPException(status_code=404, detail="No such invite")
    invite.status = "accepted"
    thread = await _open_solo_thread(session, user.id, invite.from_user_id, invite.someday_id)
    await session.commit()
    return {"thread_id": str(thread.id)}


@router.post("/invites/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invite(
    invite_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is None or invite.to_user_id != user.id:
        raise HTTPException(status_code=404, detail="No such invite")
    invite.status = "declined"
    await session.commit()


@router.post("/invites/seen", status_code=status.HTTP_204_NO_CONTENT)
async def mark_invites_seen(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    rows = (
        await session.scalars(
            select(Invite).where(Invite.to_user_id == user.id, Invite.status == "new")
        )
    ).all()
    for invite in rows:
        invite.status = "seen"
    await session.commit()


# --------------------------------------------------------------------- friends


@router.get("/friends", response_model=FriendsOut)
async def friends(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FriendsOut:
    accepted = await friend_ids(session, user.id)
    incoming = (
        await session.scalars(
            select(Friendship).where(
                Friendship.addressee_id == user.id, Friendship.status == "pending"
            )
        )
    ).all()
    outgoing = (
        await session.scalars(
            select(Friendship).where(
                Friendship.requester_id == user.id, Friendship.status == "pending"
            )
        )
    ).all()
    people = await users_by_ids(
        session,
        accepted
        | {row.requester_id for row in incoming}
        | {row.addressee_id for row in outgoing},
    )
    return FriendsOut(
        friends=[holder_out(people[uid]) for uid in accepted if uid in people],
        requests=[
            FriendRequestOut(
                id=row.id, from_user=holder_out(people[row.requester_id]), created_at=row.created_at
            )
            for row in incoming
            if row.requester_id in people
        ],
        sent=[holder_out(people[row.addressee_id]) for row in outgoing if row.addressee_id in people],
    )


@router.post("/friends/{user_id}/request", status_code=status.HTTP_201_CREATED)
async def request_friend(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    if user_id == user.id:
        raise HTTPException(status_code=422, detail="You are already you")
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such person")
    existing = await session.scalar(
        select(Friendship).where(
            or_(
                (Friendship.requester_id == user.id) & (Friendship.addressee_id == user_id),
                (Friendship.requester_id == user_id) & (Friendship.addressee_id == user.id),
            )
        )
    )
    if existing is not None:
        # An incoming pending request met with an outgoing one is agreement.
        if existing.status == "pending" and existing.requester_id == user_id:
            existing.status = "accepted"
            await session.commit()
            return {"status": "accepted"}
        return {"status": existing.status}
    session.add(Friendship(requester_id=user.id, addressee_id=user_id))
    await session.commit()
    await push.send_to(
        session,
        [user_id],
        title="New friend request",
        body=f"{user.name} wants to be friends",
        data={"kind": "friend_request"},
    )
    return {"status": "pending"}


@router.post("/friends/{user_id}/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept_friend(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.scalar(
        select(Friendship).where(
            Friendship.requester_id == user_id,
            Friendship.addressee_id == user.id,
            Friendship.status == "pending",
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No pending request")
    row.status = "accepted"
    await session.commit()


@router.post("/friends/{user_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_friend(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.scalar(
        select(Friendship).where(
            Friendship.requester_id == user_id,
            Friendship.addressee_id == user.id,
            Friendship.status == "pending",
        )
    )
    if row is not None:
        await session.delete(row)
        await session.commit()


# ---------------------------------------------------------------------- hidden


@router.get("/hidden", response_model=list[HolderOut])
async def hidden(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[HolderOut]:
    ids = await hidden_ids(session, user.id)
    people = await users_by_ids(session, ids)
    return [holder_out(person) for person in people.values()]


@router.put("/hidden/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def hide(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="No such person")
    if await session.get(HiddenUser, (user.id, user_id)) is None:
        session.add(HiddenUser(user_id=user.id, hidden_user_id=user_id))
        # Their live invites die quietly with the hide.
        rows = (
            await session.scalars(
                select(Invite).where(
                    Invite.to_user_id == user.id,
                    Invite.from_user_id == user_id,
                    Invite.status.in_(["new", "seen"]),
                )
            )
        ).all()
        for invite in rows:
            invite.status = "declined"
        await session.commit()


@router.delete("/hidden/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unhide(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(HiddenUser, (user.id, user_id))
    if row is not None:
        await session.delete(row)
        await session.commit()


# ---------------------------------------------------------------------- people


@router.get(
    "/people",
    response_model=list[HolderOut],
    dependencies=[Depends(ratelimit.limiter(ratelimit.GENERAL))],
)
async def find_people(
    q: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[HolderOut]:
    """Find someone by name or handle.

    Until this existed a new account could not reach another person at all: the
    only ways in were a group invite link or already knowing somebody's exact
    handle. Everything social the app can do - keeps becoming visible, invites
    arriving, threads - sat behind that, so half the product waited on the user
    to go and recruit a friend by other means.

    What it deliberately does not do is act as a directory. There is no listing
    without a query, the query has a floor so it cannot be walked one letter at
    a time, results are capped, and it answers on name and handle only - never
    on email, which would turn the endpoint into a way of asking "does this
    person have an account".
    """
    needle = q.strip()
    if len(needle) < SEARCH_FLOOR:
        return []
    hidden = await hidden_ids(session, user.id)
    like = f"%{needle}%"
    rows = (
        await session.scalars(
            select(User)
            .where(
                User.id != user.id,
                User.is_active.is_(True),
                or_(User.name.ilike(like), User.handle.ilike(like)),
            )
            .order_by(User.name)
            .limit(SEARCH_LIMIT)
        )
    ).all()
    return [holder_out(person) for person in rows if person.id not in hidden]


@router.get("/people/{handle}", response_model=PersonOut)
async def person(
    handle: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PersonOut:
    target = await session.scalar(
        select(User).options(selectinload(User.photos)).where(User.handle == handle)
    )
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="No such person")

    their_keeps = (
        await session.execute(
            select(Keep, Someday)
            .join(Someday, Someday.id == Keep.someday_id)
            .where(Keep.user_id == target.id)
        )
    ).all()
    mine = set(
        (await session.scalars(select(Keep.someday_id).where(Keep.user_id == user.id))).all()
    )
    somedays = [someday_out(someday) for _, someday in their_keeps]
    in_common = [someday_out(someday) for _, someday in their_keeps if someday.id in mine]

    accepted = await friend_ids(session, user.id)
    pending = await session.scalar(
        select(Friendship.id).where(
            Friendship.requester_id == user.id,
            Friendship.addressee_id == target.id,
            Friendship.status == "pending",
        )
    )
    return PersonOut(
        id=target.id,
        name=target.name,
        handle=target.handle,
        gender=target.gender,
        city=target.city,
        bio=target.bio,
        work=target.work,
        education=target.education,
        interests=target.interests,
        hometown=target.hometown,
        languages=target.languages,
        created_at=target.created_at,
        photos=[
            PhotoOut(
                id=photo.id,
                position=photo.position,
                url=storage.presign_download(photo.object_key),
            )
            for photo in target.photos
        ],
        somedays=somedays,
        in_common=in_common,
        is_friend=target.id in accepted,
        request_pending=pending is not None,
    )


@router.post("/people/{user_id}/report", status_code=status.HTTP_204_NO_CONTENT)
async def report(
    user_id: uuid.UUID,
    payload: ReportIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="No such person")
    session.add(Report(reporter_id=user.id, reported_id=user_id, reason=payload.reason))
    await session.commit()
