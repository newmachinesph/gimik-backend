"""Act on reports: python scripts/moderate.py list | eject <handle>.

App Store guideline 1.2 gives 24 hours from a report to removing the content
and ejecting whoever posted it. Every report and block is mailed to
MODERATION_EMAIL as it happens; `list` is the same queue read from the
database, and `eject` is the action.
"""

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import SessionLocal, engine
from app.models import (
    Friendship,
    GroupMember,
    Invite,
    Keep,
    Message,
    Report,
    Someday,
    ThreadParticipant,
    User,
)
from app.services import storage


async def reports(session: AsyncSession, days: int) -> list[str]:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = (
        await session.execute(
            select(Report, User)
            .join(User, User.id == Report.reported_id)
            .where(Report.created_at >= since)
            .order_by(Report.created_at.desc())
        )
    ).all()
    return [
        f"{report.created_at:%Y-%m-%d %H:%M}  @{reported.handle:<24} "
        f"{'active ' if reported.is_active else 'ejected'}  {report.reason}"
        for report, reported in rows
    ]


async def eject(session: AsyncSession, handle: str) -> bool:
    """Remove everything this person posted and end their access for good.

    The row itself stays, deactivated. Deleting it would free the email and the
    Apple or Google identity, and the same person could sign straight back up;
    kept, every one of them is still taken and `current_user` refuses the
    account on its next request.
    """
    user = await session.scalar(
        select(User).options(selectinload(User.photos)).where(User.handle == handle.lstrip("@"))
    )
    if user is None:
        return False
    keys = [photo.object_key for photo in user.photos]
    user.is_active = False
    user.token_version += 1
    user.bio = user.work = user.education = ""
    user.photos.clear()
    mine = user.id
    await session.execute(delete(Someday).where(Someday.owner_id == mine))
    await session.execute(delete(Message).where(Message.sender_id == mine))
    await session.execute(delete(Keep).where(Keep.user_id == mine))
    await session.execute(
        delete(Invite).where(or_(Invite.from_user_id == mine, Invite.to_user_id == mine))
    )
    await session.execute(
        delete(Friendship).where(
            or_(Friendship.requester_id == mine, Friendship.addressee_id == mine)
        )
    )
    await session.execute(delete(GroupMember).where(GroupMember.user_id == mine))
    await session.execute(delete(ThreadParticipant).where(ThreadParticipant.user_id == mine))
    await session.commit()
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception as error:  # noqa: BLE001 - a stranded object must not undo the ejection
            print(f"could not remove {key}: {error}")
    return True


async def main(args: argparse.Namespace) -> None:
    async with SessionLocal() as session:
        if args.command == "list":
            print("\n".join(await reports(session, args.days)) or "No reports.")
        elif await eject(session, args.handle):
            print(f"Ejected @{args.handle.lstrip('@')}: content removed, account closed.")
        else:
            print(f"No account with the handle {args.handle}.")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list").add_argument("--days", type=int, default=7)
    commands.add_parser("eject").add_argument("handle")
    asyncio.run(main(parser.parse_args()))
