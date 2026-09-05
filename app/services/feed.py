"""What to show next, out of two and a half thousand things.

The deck is the product's front door: one card, keep or pass. With a catalog
this size the question stops being "what exists" and becomes "what do we put in
front of this person, in what order, and what happens when they reach the end".
This module is the whole answer.

Four signals, blended
---------------------
**Taste** is the strongest and the only one that is really about this person: a
smoothed keep-rate per category and per subcategory, learned from what they have
already kept and passed. Smoothing matters more than the rate - after three
decisions a raw rate is noise, so the estimate starts at the global keep-rate
and is pulled toward the person's own only as evidence accumulates. Someone who
passes four hikes has said something; someone who passed one has not.

**Social** is what makes this app rather than a list app. A someday a friend has
already kept, and opened to friends, is one where keeping it produces an invite
rather than a private wish. That is worth surfacing above a marginally better
taste match, and it is why the deck is not just a recommender.

**Popularity** is the cold-start floor and nothing more. Log-damped, because the
difference between four keeps and forty is real and the difference between four
hundred and four thousand is not - undamped it turns into a chart show where the
same twenty cards win forever.

**Exploration** is a deterministic jitter seeded on (person, someday, day). Two
people with identical taste should not see an identical deck, and the same
person should not see the same order tomorrow. Deterministic rather than random
so that paging through a deck is stable while it is being paged.

Diversity is applied after scoring, not inside it
-------------------------------------------------
A pure ranking of a catalog this shape returns nine hiking cards, because if
someone likes hiking then every hiking card scores well. The greedy pass below
charges an increasing penalty for each category and subcategory already emitted,
so the page turns into a spread. This is the single biggest difference between a
deck that feels chosen and one that feels sorted.

Running out
-----------
The interesting case, and the one a naive feed gets wrong by looping. The pools
are consumed in order and each is a genuinely different answer to "there is
nothing left":

1. `catalog`      - the seeded catalog, undecided. The normal case.
2. `community`    - somedays other people wrote, but only where the author kept
                    it and opened that keep to somebody this viewer is - a
                    friend, a group peer, a neighbour in the same city. It reuses
                    the visibility rule the rest of the app already enforces, so
                    a private someday stays private.
3. `second_look`  - things passed long enough ago (SECOND_LOOK_DAYS) to be worth
                    asking again. Taste moves, and a pass made during onboarding
                    is not a life sentence. Carries a flag so the card can say so
                    rather than pretending it is new. Keeps are never resurfaced:
                    those are already on the list.
4. exhausted      - nothing left, said honestly. `FeedPage.exhausted` is set and
                    the client shows an end state. A feed that silently reshuffles
                    what someone already rejected is worse than one that admits
                    the shelf is empty.

Scale boundary
--------------
Candidates are scored in Python, which is the right call at 2,522 rows and the
wrong one at 200,000. `CANDIDATE_LIMIT` bounds the work; past roughly twenty
thousand catalog rows the scoring belongs in a precomputed table refreshed on a
schedule, keyed by (person, someday). Nothing else in this module changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Friendship, GroupMember, Keep, Pass, Someday, User

# --- weights -------------------------------------------------------------
# Tuned by hand against the seeded world rather than learned, and deliberately
# on one scale: every component below returns roughly 0..1, so these read as
# the relative importance they actually are.
W_TASTE = 1.0
W_SOCIAL = 0.8
W_POPULARITY = 0.35
W_INTEREST = 0.45
W_EXPLORE = 0.25

# How hard a repeated category (or subcategory) is charged inside one page.
#
# These have to stay well under W_TASTE, and the first attempt did not. A person
# who had kept six food cards and passed six water ones was served an exact
# food/water/food/water alternation: the penalties (0.35 + 0.22, charged
# together because every card of a category also repeated its subcategory) came
# to more than the taste gap they were competing with, so the diversity pass
# silently overruled the preference it was supposed to be decorating. A feed
# that ignores twelve explicit decisions is worse than one that shows a run.
#
# At 0.18/0.12 the same person gets roughly seven food to three water - a clear
# lean with the page still varied - while a cold account, where every category
# scores alike, still round-robins because any positive penalty is enough to
# break a tie. Cost of a repeat, not a quota.
PENALTY_CATEGORY = 0.18
PENALTY_DOMAIN = 0.12

# A resurfaced pass sits below anything fresh at a comparable score. It is a
# second chance, not a promotion.
PENALTY_SECOND_LOOK = 0.5

# Decisions' worth of global prior mixed into a per-category keep-rate. Five is
# roughly "trust the person once they have made five calls in that category".
TASTE_PRIOR = 5.0

# A pass is worth reconsidering after this long, and not before.
SECOND_LOOK_DAYS = 30

# Upper bound on rows scored per request. A no-op at today's catalog size; the
# thing that stops this module quietly becoming the slowest endpoint later.
CANDIDATE_LIMIT = 5000

CATALOG = "catalog"
COMMUNITY = "community"
SECOND_LOOK = "second_look"

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Candidate:
    """One scorable row, flattened out of the join it came from."""

    someday: Someday
    keeps: int = 0
    friend_keeps: int = 0
    pool: str = CATALOG
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class FeedPage:
    items: list[Candidate]
    exhausted: bool
    pools: dict[str, int]


@dataclass
class Taste:
    """A person's learned preferences, plus the global rates they start from."""

    category_keeps: dict[str, int]
    category_total: dict[str, int]
    domain_keeps: dict[str, int]
    domain_total: dict[str, int]
    base_rate: float
    interest_tokens: set[str]

    def _rate(self, keeps: dict, total: dict, key: str | None) -> float:
        if key is None:
            return self.base_rate
        seen = total.get(key, 0)
        kept = keeps.get(key, 0)
        return (kept + TASTE_PRIOR * self.base_rate) / (seen + TASTE_PRIOR)

    def score(self, someday: Someday) -> float:
        """Category and subcategory blended two-to-one.

        The subcategory is the sharper signal - "Hiking & Mountains" says more
        than "Adventure & Outdoors" - but it is also the sparser one, so it
        informs rather than decides.
        """
        category = self._rate(self.category_keeps, self.category_total, someday.category)
        domain = self._rate(self.domain_keeps, self.domain_total, someday.domain)
        return (2 * category + domain) / 3

    def interest_overlap(self, someday: Someday) -> float:
        """Word overlap between what someone said they like and this card.

        Interests are stored as slugs picked from a bank the app owns
        ("beach-day", "nature-walks"), so matching them by word against the
        title and subcategory costs nothing to maintain and degrades gracefully
        when the bank changes. It is a nudge, not a filter: a low weight on a
        soft signal, and behaviour overrules it the moment there is any.
        """
        if not self.interest_tokens:
            return 0.0
        text = " ".join(filter(None, [someday.title, someday.domain, someday.category]))
        tokens = set(_WORD.findall(text.lower()))
        hits = len(tokens & self.interest_tokens)
        return min(1.0, hits / 2)


def _jitter(user_id: uuid.UUID, someday_id: uuid.UUID, day: dt.date) -> float:
    """Stable within a day, different tomorrow, different per person."""
    seed = f"{user_id}:{someday_id}:{day.isoformat()}".encode()
    return int.from_bytes(hashlib.blake2b(seed, digest_size=4).digest(), "big") / 0xFFFFFFFF


def _popularity(keeps: int, ceiling: int) -> float:
    if ceiling <= 0:
        return 0.0
    return math.log1p(keeps) / math.log1p(ceiling)


async def _peer_ids(session: AsyncSession, user_id: uuid.UUID) -> tuple[set, set]:
    """Accepted friends, and everyone sharing at least one group."""
    rows = (
        await session.execute(
            select(Friendship.requester_id, Friendship.addressee_id).where(
                Friendship.status == "accepted",
                or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id),
            )
        )
    ).all()
    friends = {addressee if requester == user_id else requester for requester, addressee in rows}

    my_groups = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    peers = set(
        (
            await session.scalars(
                select(GroupMember.user_id).where(
                    GroupMember.group_id.in_(my_groups), GroupMember.user_id != user_id
                )
            )
        ).all()
    )
    return friends, peers


async def load_taste(session: AsyncSession, user: User) -> Taste:
    """Everything this person has already decided, folded into rates."""
    kept = (
        await session.execute(
            select(Someday.category, Someday.domain)
            .join(Keep, Keep.someday_id == Someday.id)
            .where(Keep.user_id == user.id)
        )
    ).all()
    passed = (
        await session.execute(
            select(Someday.category, Someday.domain)
            .join(Pass, Pass.someday_id == Someday.id)
            .where(Pass.user_id == user.id)
        )
    ).all()

    category_keeps: dict[str, int] = {}
    domain_keeps: dict[str, int] = {}
    category_total: dict[str, int] = {}
    domain_total: dict[str, int] = {}
    for category, domain in kept:
        category_keeps[category] = category_keeps.get(category, 0) + 1
        if domain:
            domain_keeps[domain] = domain_keeps.get(domain, 0) + 1
    for category, domain in list(kept) + list(passed):
        category_total[category] = category_total.get(category, 0) + 1
        if domain:
            domain_total[domain] = domain_total.get(domain, 0) + 1

    decisions = len(kept) + len(passed)
    # With no history at all the prior is "half of what you see is worth
    # keeping", which is neutral rather than optimistic.
    base_rate = len(kept) / decisions if decisions else 0.5

    tokens: set[str] = set()
    for slug in user.interests or []:
        tokens.update(_WORD.findall(str(slug).lower()))

    return Taste(
        category_keeps=category_keeps,
        category_total=category_total,
        domain_keeps=domain_keeps,
        domain_total=domain_total,
        base_rate=base_rate,
        interest_tokens=tokens,
    )


def _undecided(stmt: Select, user_id: uuid.UUID) -> Select:
    """Anti-join away anything this person has already kept or passed.

    An anti-join rather than `NOT IN (...)`: the exclusion list is unbounded in
    principle - it grows with every swipe - and inlining ten thousand ids into a
    statement is how this query stops planning well.
    """
    mine = select(Keep.someday_id).where(Keep.user_id == user_id).subquery()
    passed = select(Pass.someday_id).where(Pass.user_id == user_id).subquery()
    return (
        stmt.outerjoin(mine, mine.c.someday_id == Someday.id)
        .outerjoin(passed, passed.c.someday_id == Someday.id)
        .where(mine.c.someday_id.is_(None), passed.c.someday_id.is_(None))
    )


def _with_counts(friends: set) -> Select:
    """Base select: one row per someday, with its global and friend keep counts."""
    if friends:
        friend_keeps = func.count(Keep.user_id).filter(Keep.user_id.in_(friends))
    else:
        friend_keeps = func.count(Keep.user_id) * 0
    return (
        select(Someday, func.count(Keep.user_id).label("keeps"), friend_keeps.label("friend_keeps"))
        .outerjoin(Keep, Keep.someday_id == Someday.id)
        .group_by(Someday.id)
    )


async def _catalog_pool(
    session: AsyncSession, user: User, friends: set, category: str | None
) -> list[Candidate]:
    stmt = _undecided(_with_counts(friends), user.id).where(Someday.slug.is_not(None))
    if category:
        stmt = stmt.where(Someday.category == category)
    rows = (await session.execute(stmt.limit(CANDIDATE_LIMIT))).all()
    return [
        Candidate(someday=someday, keeps=keeps, friend_keeps=friend_keeps, pool=CATALOG)
        for someday, keeps, friend_keeps in rows
    ]


async def _community_pool(
    session: AsyncSession, user: User, friends: set, peers: set, category: str | None
) -> list[Candidate]:
    """Somedays other people wrote, filtered by the author's own visibility choice.

    A custom someday has no slug and no artwork; it is a sentence somebody typed.
    It reaches this deck only if its author kept it and opened that keep wide
    enough to include the viewer - the same friends/groups/everyone rule the
    holders list enforces. The open_to test is done here rather than in SQL
    because it is a JSON array and the portable spelling of "array contains" is
    not worth the two hundred rows it would save.
    """
    stmt = _undecided(_with_counts(friends), user.id).where(
        Someday.owner_id.is_not(None), Someday.owner_id != user.id
    )
    if category:
        stmt = stmt.where(Someday.category == category)
    rows = (await session.execute(stmt.limit(CANDIDATE_LIMIT))).all()
    if not rows:
        return []

    owner_ids = {someday.owner_id for someday, _, _ in rows}
    owners = {
        owner.id: owner
        for owner in (await session.scalars(select(User).where(User.id.in_(owner_ids)))).all()
    }
    author_keeps = {
        (keep.someday_id, keep.user_id): keep
        for keep in (
            await session.scalars(
                select(Keep).where(
                    Keep.user_id.in_(owner_ids),
                    Keep.someday_id.in_([someday.id for someday, _, _ in rows]),
                )
            )
        ).all()
    }

    out: list[Candidate] = []
    for someday, keeps, friend_keeps in rows:
        keep = author_keeps.get((someday.id, someday.owner_id))
        owner = owners.get(someday.owner_id)
        if keep is None or owner is None:
            continue
        open_to = set(keep.open_to or [])
        visible = (
            ("friends" in open_to and owner.id in friends)
            or ("groups" in open_to and owner.id in peers)
            or ("everyone" in open_to and bool(owner.city) and owner.city == user.city)
        )
        if visible:
            out.append(
                Candidate(someday=someday, keeps=keeps, friend_keeps=friend_keeps, pool=COMMUNITY)
            )
    return out


async def _second_look_pool(
    session: AsyncSession, user: User, friends: set, category: str | None
) -> list[Candidate]:
    """Passes old enough to ask about again."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=SECOND_LOOK_DAYS)
    stale = (
        select(Pass.someday_id).where(Pass.user_id == user.id, Pass.created_at < cutoff).subquery()
    )
    stmt = (
        _with_counts(friends)
        .join(stale, stale.c.someday_id == Someday.id)
        .where(Someday.slug.is_not(None))
    )
    if category:
        stmt = stmt.where(Someday.category == category)
    rows = (await session.execute(stmt.limit(CANDIDATE_LIMIT))).all()
    return [
        Candidate(someday=someday, keeps=keeps, friend_keeps=friend_keeps, pool=SECOND_LOOK)
        for someday, keeps, friend_keeps in rows
    ]


def score_candidates(candidates: list[Candidate], user: User, taste: Taste, today: dt.date) -> None:
    """Assign every candidate its score and the reasons behind it, in place."""
    ceiling = max((c.keeps for c in candidates), default=0)
    for candidate in candidates:
        someday = candidate.someday
        # Saturating at three friends: the jump from none to one is the whole
        # signal, and "nine friends kept this" is not nine times the invitation.
        social = (
            min(1.0, math.log1p(candidate.friend_keeps) / math.log(3))
            if candidate.friend_keeps
            else 0.0
        )
        interest = taste.interest_overlap(someday)
        candidate.score = (
            W_TASTE * taste.score(someday)
            + W_SOCIAL * social
            + W_POPULARITY * _popularity(candidate.keeps, ceiling)
            + W_INTEREST * interest
            + W_EXPLORE * _jitter(user.id, someday.id, today)
        )
        if candidate.pool == SECOND_LOOK:
            candidate.score -= PENALTY_SECOND_LOOK
            candidate.reasons.append("second_look")
        if candidate.friend_keeps:
            candidate.reasons.append("friends")
        if candidate.pool == COMMUNITY:
            candidate.reasons.append("community")
        if interest > 0:
            candidate.reasons.append("interest")


def diversify(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """Greedy re-rank that charges for repetition as the page fills.

    Straight `sorted(by score)` is what produces nine hiking cards in a row for
    somebody who likes hiking - each one is genuinely a good match, and together
    they are a bad page. Picking the best remaining card *after* penalising the
    categories already on the page costs one scan per slot and is the whole
    difference between a deck that feels chosen and a list that feels sorted.
    """
    if not candidates:
        return []
    pool = sorted(candidates, key=lambda c: c.score, reverse=True)[: max(limit * 20, 200)]
    chosen: list[Candidate] = []
    seen_category: dict[str, int] = {}
    seen_domain: dict[str, int] = {}

    while pool and len(chosen) < limit:
        best_index = 0
        best_value = float("-inf")
        for index, candidate in enumerate(pool):
            value = (
                candidate.score
                - PENALTY_CATEGORY * seen_category.get(candidate.someday.category, 0)
                - PENALTY_DOMAIN * seen_domain.get(candidate.someday.domain, 0)
            )
            if value > best_value:
                best_value, best_index = value, index
        picked = pool.pop(best_index)
        chosen.append(picked)
        category = picked.someday.category
        seen_category[category] = seen_category.get(category, 0) + 1
        if picked.someday.domain:
            seen_domain[picked.someday.domain] = seen_domain.get(picked.someday.domain, 0) + 1
    return chosen


async def build_feed(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 20,
    category: str | None = None,
) -> FeedPage:
    """The deck: what this person should be asked about next, and why.

    Pools are drawn in order and only as far as needed - the community and
    second-look queries do not run at all while the catalog still has enough
    left, which is the overwhelmingly common case.
    """
    friends, peers = await _peer_ids(session, user.id)
    taste = await load_taste(session, user)
    today = dt.datetime.now(dt.UTC).date()

    candidates = await _catalog_pool(session, user, friends, category)
    pools = {CATALOG: len(candidates)}

    if len(candidates) < limit:
        community = await _community_pool(session, user, friends, peers, category)
        pools[COMMUNITY] = len(community)
        candidates += community

    if len(candidates) < limit:
        second = await _second_look_pool(session, user, friends, category)
        pools[SECOND_LOOK] = len(second)
        candidates += second

    score_candidates(candidates, user, taste, today)
    items = diversify(candidates, limit)
    return FeedPage(items=items, exhausted=not items, pools=pools)
