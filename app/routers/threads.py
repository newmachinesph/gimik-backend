"""Three kinds of thread, one screen, one router. A thread can carry a someday,
a date proposal, or a place as an attachment, so "lock it" can happen inside
the conversation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import current_user
from app.models import (
    ATTACHMENT_TYPES,
    Group,
    Message,
    Someday,
    Thread,
    ThreadParticipant,
    User,
)
from app.models.base import utcnow
from app.routers.common import blocked_ids, holder_out, someday_out, users_by_ids
from app.routers.social import _open_solo_thread
from app.schemas import AttachmentIn, MessageIn, MessageOut, ThreadDetailOut, ThreadOut
from app.services import moderation, push, ratelimit, realtime

router = APIRouter(prefix="/threads", tags=["threads"])


async def _participant(session: AsyncSession, thread_id: uuid.UUID, user_id: uuid.UUID) -> ThreadParticipant:
    row = await session.get(ThreadParticipant, (thread_id, user_id))
    if row is None:
        raise HTTPException(status_code=404, detail="No such thread")
    return row


async def _title(session: AsyncSession, thread: Thread, viewer_id: uuid.UUID) -> str:
    if thread.kind == "group" and thread.group_id:
        group = await session.get(Group, thread.group_id)
        return group.name if group else "Group"
    if thread.kind == "plan" and thread.someday_id:
        someday = await session.get(Someday, thread.someday_id)
        return someday.title if someday else "Plan"
    other = next((p.user_id for p in thread.participants if p.user_id != viewer_id), None)
    if other:
        person = await session.get(User, other)
        if person:
            return person.name
    return "Thread"


async def _message_out(session: AsyncSession, message: Message, sender: User) -> MessageOut:
    return MessageOut(
        id=message.id,
        sender=holder_out(sender),
        body=message.body,
        attachment=AttachmentIn(**message.attachment) if message.attachment else None,
        created_at=message.created_at,
    )


async def _thread_out(
    session: AsyncSession,
    thread: Thread,
    viewer_id: uuid.UUID,
    blocked: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
) -> ThreadOut:
    people = await users_by_ids(session, {p.user_id for p in thread.participants})
    # The preview is the last thing the viewer is allowed to read: in a group,
    # a blocked member's message must not surface as the line under the title.
    last = next((m for m in reversed(thread.messages) if m.sender_id not in blocked), None)
    me = next((p for p in thread.participants if p.user_id == viewer_id), None)
    unread = bool(
        last
        and last.sender_id != viewer_id
        and (me is None or me.read_at is None or me.read_at < last.created_at)
    )
    someday = await session.get(Someday, thread.someday_id) if thread.someday_id else None
    return ThreadOut(
        id=thread.id,
        kind=thread.kind,
        title=await _title(session, thread, viewer_id),
        someday=someday_out(someday) if someday else None,
        plan_id=thread.plan_id,
        group_id=thread.group_id,
        participants=[holder_out(p) for p in people.values() if p.id not in blocked],
        last_message=(
            await _message_out(session, last, people[last.sender_id])
            if last and last.sender_id in people
            else None
        ),
        unread=unread,
        created_at=thread.created_at,
    )


def _loaded() -> tuple:
    return (
        selectinload(Thread.participants),
        selectinload(Thread.messages),
    )


@router.get("", response_model=list[ThreadOut])
async def my_threads(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ThreadOut]:
    threads = (
        await session.scalars(
            select(Thread)
            .join(ThreadParticipant)
            .options(*_loaded())
            .where(ThreadParticipant.user_id == user.id)
        )
    ).all()
    # A conversation with someone on either side of a block is gone from the
    # list. Group and plan threads stay; the blocked person's messages do not.
    # So is one whose other half no longer exists: a deleted or removed account
    # takes its participant row with it, and what was left listed as an empty
    # conversation called "Thread".
    blocked = await blocked_ids(session, user.id)
    threads = [
        thread
        for thread in threads
        if not (
            thread.kind == "solo"
            and (len(thread.participants) < 2 or any(p.user_id in blocked for p in thread.participants))
        )
    ]
    out = [await _thread_out(session, thread, user.id, blocked) for thread in threads]
    out.sort(
        key=lambda t: (t.last_message.created_at if t.last_message else t.created_at),
        reverse=True,
    )
    return out


@router.post("/solo/{user_id}", response_model=ThreadOut)
async def open_solo(
    user_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ThreadOut:
    if user_id == user.id:
        raise HTTPException(status_code=422, detail="A thread needs two people")
    target = await session.get(User, user_id)
    if target is None or not target.is_active or user_id in await blocked_ids(session, user.id):
        raise HTTPException(status_code=404, detail="No such person")
    thread = await _open_solo_thread(session, user.id, user_id, None)
    await session.commit()
    loaded = await session.scalar(
        select(Thread).options(*_loaded()).where(Thread.id == thread.id)
    )
    assert loaded is not None
    return await _thread_out(session, loaded, user.id)


@router.get("/{thread_id}", response_model=ThreadDetailOut)
async def thread_detail(
    thread_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ThreadDetailOut:
    await _participant(session, thread_id, user.id)
    thread = await session.scalar(
        select(Thread).options(*_loaded()).where(Thread.id == thread_id)
    )
    assert thread is not None
    people = await users_by_ids(
        session, {p.user_id for p in thread.participants} | {m.sender_id for m in thread.messages}
    )
    blocked = await blocked_ids(session, user.id)
    base = await _thread_out(session, thread, user.id, blocked)
    return ThreadDetailOut(
        **base.model_dump(exclude={"messages"}),
        messages=[
            await _message_out(session, message, people[message.sender_id])
            for message in thread.messages
            if message.sender_id in people and message.sender_id not in blocked
        ],
    )


@router.post(
    "/{thread_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.limiter(ratelimit.SOCIAL_WRITE))],
)
async def send_message(
    thread_id: uuid.UUID,
    payload: MessageIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    participant = await _participant(session, thread_id, user.id)
    if payload.attachment and payload.attachment.type not in ATTACHMENT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown attachment type")
    moderation.screen(
        payload.body, payload.attachment.label if payload.attachment else None, public=False
    )
    # Everyone in the thread except the sender, who already has it.
    others = (
        await session.scalars(
            select(ThreadParticipant.user_id).where(
                ThreadParticipant.thread_id == thread_id,
                ThreadParticipant.user_id != user.id,
            )
        )
    ).all()
    blocked = await blocked_ids(session, user.id)
    thread = await session.get(Thread, thread_id)
    # A block ends the conversation for both sides, and says nothing about why.
    if thread is not None and thread.kind == "solo" and any(o in blocked for o in others):
        raise HTTPException(status_code=404, detail="No such thread")
    message = Message(
        thread_id=thread_id,
        sender_id=user.id,
        body=payload.body.strip(),
        attachment=payload.attachment.model_dump() if payload.attachment else None,
    )
    session.add(message)
    participant.read_at = utcnow()
    await session.commit()
    out = await _message_out(session, message, user)

    # Published after the commit so realtime can never announce something that
    # rolled back, and awaited only for as long as the REST call takes. In a
    # group, nobody on either side of a block with the sender is notified.
    everyone = len(others)
    others = [recipient for recipient in others if recipient not in blocked]
    event = {
        "threadId": str(thread_id),
        "messageId": str(message.id),
        "from": user.name,
        "body": message.body,
        "at": message.created_at.isoformat(),
    }
    # The thread channel reaches every participant and carries the body, so it
    # is skipped when someone in the thread must not receive this message. The
    # per-person channels below still tell everyone else.
    if len(others) == everyone:
        await realtime.publish(realtime.thread_channel(thread_id), "message", event)
    for recipient in others:
        await realtime.publish(realtime.user_channel(recipient), "message", event)
    # Everyone in the thread but its author, on the devices they actually carry.
    await push.send_to(
        session,
        list(others),
        title=user.name,
        body=message.body[:120],
        data={"kind": "message", "threadId": str(thread_id)},
    )
    return out


@router.post("/{thread_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    thread_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    participant = await _participant(session, thread_id, user.id)
    participant.read_at = utcnow()
    await session.commit()
