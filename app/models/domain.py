"""The corpus, one table per noun.

Someday -> keep/pass -> visibility -> invite -> thread -> lock (plan) -> done ->
journal (record). People stand in exactly three relations: friend (mutual),
group member (shared membership), stranger. Everything below is that journey,
flat, the same shapes the app's store keeps on the device.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedMixin, IdMixin

# Vocabulary, enforced in the API layer; strings in the database so SQLite tests
# and Postgres share one definition.
GENDERS = {"woman", "man", "nonbinary"}
TIERS = {"free", "plus"}
PARTIES = {"solo", "duo", "group", "any"}
INVITE_PARTIES = {"duo", "group"}
AUDIENCES = {"friends", "groups", "everyone"}
INVITE_STATUSES = {"new", "seen", "accepted", "declined"}
FRIENDSHIP_STATUSES = {"pending", "accepted"}
THREAD_KINDS = {"solo", "group", "plan"}
SPLITS = {"kkb", "equal", "treat", "others"}
TIME_SLOTS = {"Morning", "Afternoon", "Evening"}
ATTACHMENT_TYPES = {"someday", "date", "place"}
# How a someday finishes. "single" is one evening and is the overwhelming
# majority; the rest need progress kept against them (see SomedayProgress).
COMPLETIONS = {"single", "count", "distance", "duration", "streak", "percentage",
               "custom", "all_members"}
# When something is actually pleasant here. Deliberately coarse - the Philippines
# runs on two seasons, not four - and nullable, because it is unknown for most of
# the catalog rather than assumed.
SEASONS = {"dry", "wet", "any"}
# Rough cost to take part, per person. Nullable for the same reason.
COST_BANDS = {"free", "low", "mid", "high"}


class User(Base, IdMixin, CreatedMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # Null for an account created through Google: there is no password to
    # store, and a sentinel string would be a lie every caller has to decode.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google's stable subject claim, which outlives an email change. Unique so
    # one Google account can only ever reach one user; null until it is used.
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # Apple's stable subject, same reasoning as google_sub.
    apple_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # Kept only so account deletion can revoke it, as App Store rules require.
    apple_refresh_token: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    handle: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    # Set once at sign-up and never editable afterwards (corpus 9).
    birthday: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bio: Mapped[str] = mapped_column(String(280), default="", nullable=False)
    # Bumble-style headline facts, both optional.
    work: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    education: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # Need slugs worn as emoji badges. Interests only - no zodiac, religion,
    # politics, or body stats, ever.
    interests: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    hometown: Mapped[str | None] = mapped_column(String(80), nullable=True)
    languages: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    religion: Mapped[str | None] = mapped_column(String(30), nullable=True)
    politics: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether the address has been proven. Deliberately not a gate on sign-in:
    # an unproven address costs nothing until it is used to reach someone.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Bumped to invalidate every token already issued for this account. JWTs are
    # stateless, so this integer is the only thing that can end a session early:
    # a password change, a reset, or a sign-out-everywhere all move it forward
    # and every token minted before it stops decoding.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Entitlement. Written by the RevenueCat webhook, never by the client - a
    # paywall the app enforces is a paywall a patched build walks through, and
    # the bill for the request it lets past arrives here rather than there.
    # `plus_until` is the source of truth: `tier` is a label, and an expired
    # subscription is one whose timestamp has passed, not one someone remembered
    # to downgrade.
    tier: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    plus_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_plus(self) -> bool:
        """Entitled right now, regardless of what the label says."""
        if self.plus_until is None:
            return False
        expiry = self.plus_until
        # SQLite hands back naive datetimes; Postgres does not. Compare in UTC.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.UTC)
        return expiry > dt.datetime.now(dt.UTC)

    photos: Mapped[list[UserPhoto]] = relationship(
        back_populates="user", order_by="UserPhoto.position", cascade="all, delete-orphan"
    )


class UserPhoto(Base, IdMixin, CreatedMixin):
    """Up to eight; position 0 is the face everywhere. Objects live in the
    private media bucket and are served by presigned URL."""

    __tablename__ = "user_photos"
    __table_args__ = (UniqueConstraint("user_id", "position"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="photos")


class Someday(Base, IdMixin, CreatedMixin):
    """A catalog concept (slug set, artwork in the catalog bucket) or a custom
    sentence someone typed (owner set, no slug, no artwork)."""

    __tablename__ = "somedays"

    slug: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(60), nullable=True)
    register: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party: Mapped[str] = mapped_column(String(10), default="any", nullable=False)
    location: Mapped[str | None] = mapped_column(String(20), nullable=True)
    needs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Authored in the catalog and, until now, dropped by the seeder. Most rows
    # are "single"; the handful that are not are the difference between a
    # challenge you can track and a card that lies about being finishable.
    completion: Mapped[str] = mapped_column(String(16), default="single", nullable=False)
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Both nullable and both mostly empty on purpose. The mechanism to rank on
    # season and to filter on cost is worth having; inventing a season and a
    # price for 2,522 real activities is not, because somebody plans an
    # afternoon around a number that was guessed. Populate from real data.
    season: Mapped[str | None] = mapped_column(String(8), nullable=True)
    cost_band: Mapped[str | None] = mapped_column(String(8), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Keep(Base, CreatedMixin):
    """The swipe right, with its two visibility axes asked at the same moment:
    who may see it (open_to) and in what shape (party_as)."""

    __tablename__ = "keeps"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), primary_key=True
    )
    open_to: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    party_as: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class Pass(Base, CreatedMixin):
    __tablename__ = "passes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), primary_key=True
    )


class Invite(Base, IdMixin, CreatedMixin):
    """Someone asking to do one of your somedays with you, as duo or group.
    Declining is quiet; nobody is told (corpus 4)."""

    __tablename__ = "invites"
    __table_args__ = (Index("ix_invites_to_user_status", "to_user_id", "status"),)

    from_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), nullable=False
    )
    party: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="new", nullable=False)


class Friendship(Base, IdMixin, CreatedMixin):
    """Mutual by definition: a pending row is a request, an accepted row is a
    friendship, and nothing else exists."""

    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("requester_id", "addressee_id"),)

    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    addressee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)


class HiddenUser(Base, CreatedMixin):
    """Hide {name}: their invites stop for good, reversibly, silently."""

    __tablename__ = "hidden_users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    hidden_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class Group(Base, IdMixin, CreatedMixin):
    """Membership, not friendship: whoever opens the link is in, and members can
    invite each other to Groups-visible somedays without being friends."""

    __tablename__ = "groups"

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    invite_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    members: Mapped[list[GroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base, CreatedMixin):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    group: Mapped[Group] = relationship(back_populates="members")


class Place(Base, IdMixin, CreatedMixin):
    """Plan-compatible identity. Open-data provenance lives in CatalogPlace.

    Google rows keep only IDs and short-lived coordinates; prediction labels
    are response-only. They are never reusable catalogue entries.
    """

    __tablename__ = "places"
    __table_args__ = (UniqueConstraint("provider", "provider_id"),)

    provider: Mapped[str] = mapped_column(String(20), default="google", nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    area: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    lat: Mapped[float | None] = mapped_column(nullable=True)
    lon: Mapped[float | None] = mapped_column(nullable=True)
    coordinates_refreshed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refreshed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )


class Plan(Base, IdMixin, CreatedMixin):
    """A locked someday: date, people, place, split, thread. Marking it done
    stamps done_at and the someday joins the record."""

    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_owner_someday", "owner_id", "someday_id"),)

    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    time_slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The label as the person said it ("Toyo Eatery · Chino Roces Ave"); the
    # optional FK points at the resolved place when the label came from a pick.
    place: Mapped[str | None] = mapped_column(String(200), nullable=True)
    place_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    split: Mapped[str] = mapped_column(String(10), default="kkb", nullable=False)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    people: Mapped[list[PlanPerson]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlanPerson(Base, CreatedMixin):
    __tablename__ = "plan_people"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="people")


class Thread(Base, IdMixin, CreatedMixin):
    """Three kinds, one shape: solo (participants are two people), group (the
    group's members), plan (the plan's people, plan card pinned). A someday can
    be pinned to any of them - the invite that started it, or the plan."""

    __tablename__ = "threads"

    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=True
    )
    someday_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("somedays.id", ondelete="SET NULL"), nullable=True
    )

    participants: Mapped[list[ThreadParticipant]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="thread", order_by="Message.created_at", cascade="all, delete-orphan"
    )


class ThreadParticipant(Base, CreatedMixin):
    __tablename__ = "thread_participants"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    thread: Mapped[Thread] = relationship(back_populates="participants")


class Message(Base, IdMixin, CreatedMixin):
    """A body plus at most one attachment - a someday, a date proposal, or a
    place - so "lock it" can happen inside the conversation."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_thread_created", "thread_id", "created_at"),)

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # {"type": "someday"|"date"|"place", "ref": str, "label": str}
    attachment: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    thread: Mapped[Thread] = relationship(back_populates="messages")


class JournalEntry(Base, IdMixin, CreatedMixin):
    """A private memory: photo and a few lines, after."""

    __tablename__ = "journal_entries"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    photo_key: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PushToken(Base, IdMixin, CreatedMixin):
    """Where to reach someone when the app is closed.

    The whole loop waits on another person answering an invite, and until now
    that message only existed inside a running app - Ably delivers to a live
    connection and nowhere else. An invite arriving while the phone was in a
    pocket stayed invisible until its recipient happened to open Gimik, and
    invites lapse after a week.

    One row per device, keyed by the Expo push token. A token is not a secret
    but it is a way to reach a person, so it is deleted on sign-out rather than
    left behind on a shared handset.
    """

    __tablename__ = "push_tokens"
    __table_args__ = (UniqueConstraint("token"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(10), default="android", nullable=False)
    # Bumped whenever the device checks in, so a token nobody has presented in
    # months can be swept without guessing.
    seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )


class SomedayProgress(Base, CreatedMixin):
    """How far along a someday that cannot be finished in one sitting.

    Most somedays are done once and that is that. A few are not: the catalog has
    always carried a `completion` of count, distance, duration, streak or
    percentage on twenty-nine concepts - "100 islands in a year", "30 day plank
    challenge" - and the seeder dropped that column on the floor, so the app
    offered them as though a single evening would finish them.

    `count` is whatever the unit is (islands, days, kilometres); `target` is
    copied from the someday when progress starts, so moving the catalog's
    goalposts later cannot silently rewrite somebody's history.
    """

    __tablename__ = "someday_progress"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("somedays.id", ondelete="CASCADE"), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Report(Base, IdMixin, CreatedMixin):
    """Quiet, quick, and never shown to them."""

    __tablename__ = "reports"

    reporter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reported_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
