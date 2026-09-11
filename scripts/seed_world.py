"""Seed the demo world: the nine seeded people, and one account that knows them.

The app has always had a world - nine people with cities, bios, taste and
overlapping lists - but it lived only in `gora-v2/src/lib/people.ts`, so an
account created against this API opened onto nothing. This mirrors that world
into the database so the API can be developed and demonstrated against the same
cast the demo store uses, and so an integration walkthrough shows the same
screens with the same names on them.

Idempotent: everything is upserted by a natural key (email, slug, token), so
running it twice changes nothing.

    python scripts/seed_world.py [--reset]

--reset deletes the demo users and everything hanging off them first, which is
what you want after changing the shape of the world rather than its contents.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import (
    Friendship,
    Group,
    GroupMember,
    Invite,
    JournalEntry,
    Keep,
    Message,
    Plan,
    PlanPerson,
    Someday,
    Thread,
    ThreadParticipant,
    User,
)
from app.security import hash_password

# Everyone in the demo world shares this. It is not a secret and the seed is
# never run against a real database - `--reset` would be catastrophic there.
DEMO_PASSWORD = "gimik-demo-1234"
# Not a reserved TLD: pydantic's EmailStr rejects .test/.example outright.
DEMO_DOMAIN = "gimik.ph"

UTC = dt.UTC


def ago(hours: float) -> dt.datetime:
    return dt.datetime.now(UTC) - dt.timedelta(hours=hours)


def day(offset: int) -> dt.date:
    return (dt.datetime.now(UTC) + dt.timedelta(days=offset)).date()


def birthday_for(age: int) -> dt.date:
    today = dt.datetime.now(UTC).date()
    return today.replace(year=today.year - age)


# The cast, transcribed from gora-v2/src/lib/people.ts. `leans` are the
# categories they say yes to more often, and are what their keeps are drawn
# from - the overlap the app shows is a real intersection, not a number.
PEOPLE = [
    dict(handle="mara", name="Mara Ilagan", gender="woman", age=27, city="Cebu City",
         bio="Weekend diver. Will drive three hours for a good sunrise.",
         work="Marine biologist, Oceana", education="BS Biology, UP Cebu",
         hometown="Dumaguete", languages=["bisaya", "filipino", "english"],
         leans=["Nature", "Adventure", "Water"], photos=5),
    dict(handle="jon", name="Jon Reyes", gender="man", age=31, city="Makati",
         bio="Eats his way through every city. Ask me about lechon.",
         work="Sous chef, Toyo Eatery", education="Culinary Arts, Enderun",
         hometown="Batangas City", languages=["filipino", "english"],
         leans=["Food", "Cities", "Culture"], photos=4),
    dict(handle="ana", name="Ana Cruz", gender="woman", age=25, city="Lapu-Lapu",
         bio="Freediver, beach cleanup regular, terrible at karaoke.",
         work="Dive instructor, Freedive Cebu", education="BS Tourism, USC",
         hometown="Lapu-Lapu", languages=["bisaya", "english"],
         leans=["Water", "Community", "Fitness"], photos=5),
    dict(handle="theo", name="Theo Lim", gender="man", age=29, city="Quezon City",
         bio="Learning things for the sake of it. Currently: pottery.",
         work="Software engineer, Canva", education="BS Computer Science, Ateneo",
         hometown="Baguio", languages=["filipino", "english", "mandarin"],
         leans=["Skills", "Culture", "Cities"], photos=3),
    dict(handle="noor", name="Noor Salcedo", gender="woman", age=33, city="Davao City",
         bio="Trail runner who plans trips around markets.",
         work="Physical therapist", education="BS PT, San Pedro College",
         hometown="Cotabato City", languages=["maguindanaon", "filipino", "english"],
         leans=["Food", "Nature"], photos=4),
    dict(handle="kaz", name="Kaz Miyamoto", gender="man", age=28, city="Taguig",
         bio="Runs, climbs, and photographs cities at night.",
         work="Photographer, freelance", education="BFA, UST",
         hometown="Osaka", languages=["japanese", "english", "filipino"],
         leans=["Cities", "Skills", "Fitness"], photos=5),
    dict(handle="ivy", name="Ivy Tanaka", gender="nonbinary", age=26, city="Iloilo City",
         bio="Museums, folk art, and long train rides.",
         work="Museum programs officer, ILOMOCA", education="AB Art Studies, UP Visayas",
         hometown="Iloilo City", languages=["hiligaynon", "filipino", "english"],
         leans=["Culture", "Community"], photos=3),
    dict(handle="ren", name="Ren Ocampo", gender="man", age=30, city="Baguio",
         bio="Mountain weekends. Coffee first.",
         work="Mountain guide, Cordillera Treks", education="BS Forestry, Benguet State",
         hometown="La Trinidad", languages=["ilocano", "filipino", "english"],
         leans=["Adventure", "Fitness", "Water"], photos=4),
    dict(handle="dani", name="Dani Villar", gender="woman", age=27, city="Manila",
         bio="Keeps a long list and a short calendar.",
         work="Project manager, WWF PH", education="BS Management, La Salle",
         hometown="Naga", languages=["bikol", "filipino", "english"],
         leans=["Nature", "Community", "Skills"], photos=4),
]

# The account the walkthrough signs in as: a life a week in, matching the
# frontend's `week-in` demo state so both sides show the same screens.
ME = dict(handle="bea", name="Bea", gender="woman", age=28, city="Cebu City",
          bio="Saying yes to more weekends.", work="Dive instructor",
          education="BS Tourism, USC", hometown="Cebu City",
          languages=["bisaya", "filipino", "english"],
          interests=["spa-day", "surfing", "freediving", "beach-day"])


# Bea's twelve, chosen so the overlap with the cast is real: the first two get
# dates, the last is already done and in the record.
MY_SLUGS = [
    "see-an-aurora", "ride-a-hot-air-balloon", "eat-street-food",
    "dive-a-coral-reef", "learn-to-surf", "climb-to-a-mountain-summit",
    "camp-under-the-stars", "attend-an-outdoor-festival",
    "volunteer-at-a-community-garden", "kayak-through-a-river-canyon",
    "learn-to-play-a-musical-instrument", "run-a-long-distance-course",
]
DONE_SLUG = "explore-a-desert-landscape"

# Keyed on handle rather than email: the handle is the stable identity, and
# matching on email alone would strand a cast seeded under an older domain.
DEMO_HANDLES = [person["handle"] for person in PEOPLE] + [ME["handle"]]


async def reset(session) -> int:
    """Remove the demo cast and everything reachable from it."""
    users = (await session.scalars(select(User).where(User.handle.in_(DEMO_HANDLES)))).all()
    if not users:
        return 0
    ids = [user.id for user in users]
    groups = (await session.scalars(select(Group).where(Group.created_by.in_(ids)))).all()
    group_ids = [group.id for group in groups]
    threads = (await session.scalars(
        select(Thread).join(ThreadParticipant).where(ThreadParticipant.user_id.in_(ids))
    )).unique().all()
    thread_ids = [thread.id for thread in threads]
    plans = (await session.scalars(select(Plan).where(Plan.owner_id.in_(ids)))).all()
    plan_ids = [plan.id for plan in plans]

    for statement in (
        delete(Message).where(Message.thread_id.in_(thread_ids)),
        delete(ThreadParticipant).where(ThreadParticipant.thread_id.in_(thread_ids)),
        delete(Thread).where(Thread.id.in_(thread_ids)),
        delete(PlanPerson).where(PlanPerson.plan_id.in_(plan_ids)),
        delete(Plan).where(Plan.id.in_(plan_ids)),
        delete(JournalEntry).where(JournalEntry.user_id.in_(ids)),
        delete(GroupMember).where(GroupMember.group_id.in_(group_ids)),
        delete(Group).where(Group.id.in_(group_ids)),
        delete(Invite).where(Invite.to_user_id.in_(ids) | Invite.from_user_id.in_(ids)),
        delete(Friendship).where(
            Friendship.requester_id.in_(ids) | Friendship.addressee_id.in_(ids)
        ),
        delete(Keep).where(Keep.user_id.in_(ids)),
        delete(User).where(User.id.in_(ids)),
    ):
        await session.execute(statement)
    await session.commit()
    return len(ids)


async def upsert_user(session, spec: dict, *, interests: list[str] | None = None) -> User:
    email = f"{spec['handle']}@{DEMO_DOMAIN}"
    user = await session.scalar(select(User).where(User.handle == spec["handle"]))
    if user is None:
        user = User(email=email, handle=spec["handle"], password_hash=hash_password(DEMO_PASSWORD))
        session.add(user)
    user.email = email
    user.password_hash = hash_password(DEMO_PASSWORD)
    user.name = spec["name"]
    user.gender = spec["gender"]
    user.birthday = birthday_for(spec["age"])
    user.city = spec["city"]
    user.bio = spec["bio"]
    user.work = spec["work"]
    user.education = spec["education"]
    user.hometown = spec["hometown"]
    user.languages = spec["languages"]
    user.interests = interests or []
    await session.flush()
    return user


async def seed_world(*, do_reset: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with SessionLocal() as session:
        if do_reset:
            counts["reset"] = await reset(session)

        somedays = (await session.scalars(select(Someday))).all()
        if not somedays:
            raise SystemExit("catalog is empty - run scripts/seed.py first")
        by_slug = {s.slug: s for s in somedays if s.slug}
        # The cast and Bea's list were written against the first catalog, whose
        # category names were single words and whose slugs were phrases. A later
        # export renamed both. Rather than freeze the demo to one export, the
        # names below are resolved against whatever catalog is actually loaded,
        # and anything that does not resolve falls back to a deterministic pick
        # so the demo is never silently empty - which is what "keeps_cast: 0"
        # meant the first time this ran against the 2,000-activity export.
        CATEGORY_ALIASES = {
            "Nature": "Nature & Countryside",
            "Adventure": "Adventure & Outdoors",
            "Water": "Water & Islands",
            "Food": "Food & Drink",
            "Cities": "City & Social",
            "Culture": "Culture & Heritage",
            "Community": "City & Social",
            "Fitness": "Wellness & Sports",
            "Skills": "Travel & Learning",
        }

        by_category: dict[str, list[Someday]] = {}
        for someday in sorted(somedays, key=lambda s: s.slug or ""):
            by_category.setdefault(someday.category, []).append(someday)

        people = {spec["handle"]: await upsert_user(session, spec) for spec in PEOPLE}
        me = await upsert_user(session, ME, interests=ME["interests"])
        counts["users"] = len(people) + 1

        async def keep(user: User, someday: Someday, *, hours: float, open_to=("friends",),
                       party_as=("duo", "group")) -> None:
            existing = await session.scalar(
                select(Keep).where(Keep.user_id == user.id, Keep.someday_id == someday.id)
            )
            if existing is None:
                session.add(Keep(user_id=user.id, someday_id=someday.id,
                                 open_to=list(open_to), party_as=list(party_as),
                                 created_at=ago(hours)))

        # The cast keeps from the categories they lean into, so "somedays in
        # common" and "who's in" are real intersections rather than decoration.
        kept = 0
        for spec in PEOPLE:
            person = people[spec["handle"]]
            for category in spec["leans"]:
                resolved = category if category in by_category else CATEGORY_ALIASES.get(category, category)
                for someday in by_category.get(resolved, [])[:8]:
                    await keep(person, someday, hours=24 * 30, open_to=("friends", "everyone"))
                    kept += 1
        # Everyone also holds a few of Bea's twelve, which is what makes the
        # overlap on her screens non-empty.
        for index, slug in enumerate(MY_SLUGS):
            someday = by_slug.get(slug)
            if someday is None:
                continue
            for spec in PEOPLE[index % 3 :: 3]:
                await keep(people[spec["handle"]], someday, hours=24 * 20,
                           open_to=("friends", "everyone"))
                kept += 1
        counts["keeps_cast"] = kept

        mine = [by_slug[slug] for slug in MY_SLUGS if slug in by_slug]
        if len(mine) < len(MY_SLUGS):
            # One from each category, in a fixed order, so a re-run keeps the
            # same twelve and screenshots stay comparable between seeds.
            chosen = {s.id for s in mine}
            for category in sorted(by_category):
                for someday in by_category[category]:
                    if someday.id not in chosen:
                        mine.append(someday)
                        chosen.add(someday.id)
                        break
                if len(mine) >= len(MY_SLUGS):
                    break
        for index, someday in enumerate(mine):
            await keep(me, someday, hours=24 * (len(mine) - index))
        done_someday = by_slug.get(DONE_SLUG)
        if done_someday is not None:
            await keep(me, done_someday, hours=24 * 20)
        counts["keeps_mine"] = len(mine) + (1 if done_someday else 0)
        await session.flush()

        # Friends are mutual and requests are not: two accepted, two pending.
        async def friendship(a: User, b: User, status: str, hours: float) -> None:
            existing = await session.scalar(
                select(Friendship).where(
                    Friendship.requester_id == a.id, Friendship.addressee_id == b.id
                )
            )
            if existing is None:
                session.add(Friendship(requester_id=a.id, addressee_id=b.id,
                                       status=status, created_at=ago(hours)))

        await friendship(me, people["mara"], "accepted", 24 * 9)
        await friendship(me, people["ren"], "accepted", 24 * 6)
        await friendship(people["ana"], me, "pending", 6)
        await friendship(people["kaz"], me, "pending", 20)
        counts["friendships"] = 4
        await session.flush()

        # One group. Membership is not friendship: Jon and Ivy are in it and are
        # not friends, which is exactly the case the corpus wants demonstrable.
        group = await session.scalar(select(Group).where(Group.invite_token == "saturday"))
        if group is None:
            group = Group(name="Saturday People", invite_token="saturday",
                          created_by=me.id, created_at=ago(24 * 9))
            session.add(group)
            await session.flush()
            for member in (me, people["jon"], people["ren"], people["ivy"]):
                session.add(GroupMember(group_id=group.id, user_id=member.id))
        counts["groups"] = 1
        await session.flush()

        # Six invites waiting on Bea. The last is deliberately 152 hours old so
        # the sub-48h "16h left" caption has something to describe.
        invite_specs = [
            ("mara", "see-an-aurora", "group", 2.0, "new"),
            ("jon", "ride-a-hot-air-balloon", "duo", 7.0, "new"),
            ("ana", "eat-street-food", "duo", 12.0, "new"),
            ("theo", "dive-a-coral-reef", "group", 17.0, "seen"),
            ("noor", "climb-to-a-mountain-summit", "duo", 22.0, "seen"),
            ("kaz", "kayak-through-a-river-canyon", "duo", 152.0, "seen"),
        ]
        made = 0
        for handle, slug, party, hours, status in invite_specs:
            someday = by_slug.get(slug)
            if someday is None:
                continue
            sender = people[handle]
            existing = await session.scalar(
                select(Invite).where(Invite.from_user_id == sender.id,
                                     Invite.to_user_id == me.id,
                                     Invite.someday_id == someday.id)
            )
            if existing is None:
                session.add(Invite(from_user_id=sender.id, to_user_id=me.id,
                                   someday_id=someday.id, party=party,
                                   status=status, created_at=ago(hours)))
                made += 1
        counts["invites"] = made
        await session.flush()

        async def thread_for(kind: str, members: list[User], *, someday=None, plan_id=None,
                             group_id=None, hours: float = 24.0) -> Thread:
            query = select(Thread).where(Thread.kind == kind)
            if group_id is not None:
                query = query.where(Thread.group_id == group_id)
            if plan_id is not None:
                query = query.where(Thread.plan_id == plan_id)
            if someday is not None and plan_id is None and group_id is None:
                query = query.where(Thread.someday_id == someday.id)
            found = await session.scalar(query)
            if found is not None:
                return found
            thread = Thread(kind=kind, group_id=group_id, plan_id=plan_id,
                            someday_id=someday.id if someday is not None else None,
                            created_at=ago(hours))
            session.add(thread)
            await session.flush()
            for member in members:
                session.add(ThreadParticipant(thread_id=thread.id, user_id=member.id))
            return thread

        # Two locked plans: one this week with Mara, one a fortnight out.
        plan_specs = [
            ("see-an-aurora", 2, "09:00", ["mara"]),
            ("ride-a-hot-air-balloon", 16, None, ["jon", "ren"]),
        ]
        for slug, offset, slot, with_handles in plan_specs:
            someday = by_slug.get(slug)
            if someday is None:
                continue
            plan = await session.scalar(
                select(Plan).where(Plan.owner_id == me.id, Plan.someday_id == someday.id)
            )
            if plan is None:
                plan = Plan(someday_id=someday.id, owner_id=me.id, date=day(offset),
                            time_slot=slot, split="kkb", created_at=ago(24 * 2))
                session.add(plan)
                await session.flush()
                for handle in with_handles:
                    session.add(PlanPerson(plan_id=plan.id, user_id=people[handle].id,
                                           confirmed=handle == with_handles[0]))
                await thread_for("plan", [me] + [people[h] for h in with_handles],
                                 someday=someday, plan_id=plan.id)
        counts["plans"] = len(plan_specs)
        await session.flush()

        # The done one, with the memory that goes under it.
        if done_someday is not None:
            done_plan = await session.scalar(
                select(Plan).where(Plan.owner_id == me.id, Plan.someday_id == done_someday.id)
            )
            if done_plan is None:
                done_plan = Plan(someday_id=done_someday.id, owner_id=me.id,
                                 date=day(-3), split="kkb", done_at=ago(24 * 3),
                                 created_at=ago(24 * 10))
                session.add(done_plan)
                await session.flush()
            entry = await session.scalar(
                select(JournalEntry).where(JournalEntry.user_id == me.id,
                                           JournalEntry.someday_id == done_someday.id)
            )
            if entry is None:
                session.add(JournalEntry(
                    user_id=me.id, someday_id=done_someday.id,
                    body="Colder than expected. Would do it again tomorrow.",
                    created_at=ago(24 * 3)))
            counts["done"] = 1

        # A solo thread with an unanswered message in it, and the group's own.
        solo = await thread_for("solo", [me, people["mara"]],
                                someday=by_slug.get("see-an-aurora"), hours=24 * 3)
        if not (await session.scalars(select(Message).where(Message.thread_id == solo.id))).all():
            session.add(Message(thread_id=solo.id, sender_id=people["mara"].id,
                                body="Saturday works for me if the tide is right. You?",
                                created_at=ago(3)))
        group_thread = await thread_for(
            "group", [me, people["jon"], people["ren"], people["ivy"]],
            group_id=group.id, hours=24 * 9)
        if not (await session.scalars(
            select(Message).where(Message.thread_id == group_thread.id)
        )).all():
            session.add(Message(thread_id=group_thread.id, sender_id=people["jon"].id,
                                body="Who is in for the canyon kayak next month?",
                                created_at=ago(28)))
            session.add(Message(thread_id=group_thread.id, sender_id=people["ren"].id,
                                body="Me. I can bring a second boat.",
                                created_at=ago(26)))
        counts["threads"] = 3

        await session.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete the demo cast first (never run against real data)")
    args = parser.parse_args()
    counts = asyncio.run(seed_world(do_reset=args.reset))
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"\nsign in as {ME['handle']}@{DEMO_DOMAIN} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
