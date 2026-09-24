"""Wire shapes. They mirror the app store's types one for one, because the
store's docstring promised exactly that: flat objects, id lists, no nesting a
screen would have to unpick."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------- auth / users


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    birthday: dt.date | None = None
    gender: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class GoogleSignInIn(BaseModel):
    """The ID token Google minted for the app. Verified server-side, never trusted."""

    id_token: str = Field(min_length=1, max_length=8192)


class AppleSignInIn(BaseModel):
    """What Sign in with Apple hands the app. The token is verified, never trusted.

    `name` is the only field not inside the token: Apple gives it to the app on
    the first authorization and never again.
    """

    identity_token: str = Field(min_length=1, max_length=8192)
    authorization_code: str | None = Field(default=None, max_length=1024)
    name: str | None = Field(default=None, max_length=80)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - a scheme name, not a secret


class RefreshIn(BaseModel):
    refresh_token: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class EmailIn(BaseModel):
    """An address on its own - resending a verification link."""

    email: EmailStr


class VerifyEmailIn(BaseModel):
    token: str


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)


class PhotoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    url: str


class ProfileOut(BaseModel):
    id: uuid.UUID
    email: EmailStr | None = None
    # Only ever populated for the caller's own profile, alongside the address.
    email_verified: bool = False
    # "free" or "plus", derived from `plus_until` rather than stored blindly -
    # the app reads this to decide what to show, never to decide what to allow.
    tier: str = "free"
    plus_until: dt.datetime | None = None
    name: str
    handle: str
    birthday: dt.date | None
    gender: str | None
    city: str | None
    bio: str
    work: str = ""
    education: str = ""
    interests: list[str] = []
    hometown: str | None = None
    languages: list[str] = []
    religion: str | None = None
    politics: str | None = None
    created_at: dt.datetime
    photos: list[PhotoOut] = []


class ProfilePatch(BaseModel):
    """Name, birthday and gender are absent on purpose: declared once at
    sign-up, never edited (corpus 9, Bumble rule)."""

    city: str | None = Field(default=None, max_length=80)
    bio: str | None = Field(default=None, max_length=280)
    work: str | None = Field(default=None, max_length=120)
    education: str | None = Field(default=None, max_length=120)
    interests: list[str] | None = None
    hometown: str | None = Field(default=None, max_length=80)
    languages: list[str] | None = None
    religion: str | None = Field(default=None, max_length=30)
    politics: str | None = Field(default=None, max_length=30)


# -------------------------------------------------------------------- somedays


class SomedayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str | None
    title: str
    category: str
    # The subcategory ("Hiking & Mountains"). The feed ranks on it, and a card
    # that shows it reads as placed rather than loose in a category of 400.
    domain: str | None = None
    # "single" for almost everything. Anything else is a thing you chip at, and
    # the card has to say so rather than offering a Lock-a-date button for
    # "100 islands in a year".
    completion: str = "single"
    target: int | None = None
    party: str
    needs: list[str]
    artwork_url: str | None = None


class FeedItemOut(BaseModel):
    """A card, plus why it is here.

    `reasons` is what lets the deck explain itself - "2 friends want this",
    "worth another look" - which is both better product and the only practical
    way to tell a mis-ranked feed from a working one from the outside.
    """

    someday: SomedayOut
    pool: str
    reasons: list[str] = Field(default_factory=list)
    # How many people hold this, and how many of them the viewer knows. The card
    # says "2 friends want this" rather than a number invented on the client -
    # which is what it did while the only holder table was the bundled demo one.
    keeps: int = 0
    friend_keeps: int = 0


class FeedOut(BaseModel):
    items: list[FeedItemOut]
    # True when every pool is empty: the client shows an end state rather than
    # asking again and getting the same nothing.
    exhausted: bool
    pools: dict[str, int] = Field(default_factory=dict)


class ProgressIn(BaseModel):
    """One more island, day, or kilometre. Negative to correct a mistake."""

    step: int = Field(default=1, ge=-1000, le=1000)


class ProgressOut(BaseModel):
    someday_id: uuid.UUID
    count: int
    target: int | None
    last_at: dt.datetime | None
    # True once count has met the target - or, when a concept has no number
    # attached, once the person says so by marking the plan done.
    complete: bool


class PushTokenIn(BaseModel):
    """A device asking to be reachable when the app is not open."""

    token: str = Field(min_length=8, max_length=255)
    platform: str = Field(default="android", max_length=10)


class CatalogPage(BaseModel):
    """One page of browse results. `total` drives the count line and the client
    stops paging when `next_offset` is null."""

    items: list[SomedayOut]
    total: int
    next_offset: int | None = None


class Visibility(BaseModel):
    open_to: list[str] = Field(default_factory=lambda: ["friends"])
    party_as: list[str] = Field(default_factory=lambda: ["duo", "group"])


class KeepIn(Visibility):
    pass


class SomedayCreate(Visibility):
    """A someday somebody wrote themselves, plus who it is open to.

    Visibility is set at creation rather than after, because a custom someday is
    kept by its author the moment it exists - writing it down *is* wanting it -
    and a keep is what carries visibility. Creating one silently un-open would
    make the author the only person who could ever see it.
    """

    title: str = Field(min_length=3, max_length=200)
    category: str


class KeepOut(BaseModel):
    someday: SomedayOut
    open_to: list[str]
    party_as: list[str]
    kept_at: dt.datetime
    # Only ever set for a someday that cannot be finished in one sitting, which
    # keeps a list of ordinary keeps from carrying a column of nulls.
    progress: ProgressOut | None = None


class HolderOut(BaseModel):
    id: uuid.UUID
    name: str
    handle: str
    gender: str | None
    city: str | None


# --------------------------------------------------------------------- invites


class InviteCreate(BaseModel):
    to_user_id: uuid.UUID
    someday_id: uuid.UUID
    party: str


class InviteOut(BaseModel):
    id: uuid.UUID
    from_user: HolderOut
    someday: SomedayOut
    party: str
    status: str
    created_at: dt.datetime


# --------------------------------------------------------------------- friends


class FriendRequestOut(BaseModel):
    id: uuid.UUID
    from_user: HolderOut
    created_at: dt.datetime


class FriendsOut(BaseModel):
    friends: list[HolderOut]
    requests: list[FriendRequestOut]
    sent: list[HolderOut]


# ---------------------------------------------------------------------- groups


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    invite_token: str
    members: list[HolderOut]
    created_at: dt.datetime


# --------------------------------------------------------------------- threads


class AttachmentIn(BaseModel):
    type: str
    ref: str
    label: str


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    attachment: AttachmentIn | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    sender: HolderOut
    body: str
    attachment: AttachmentIn | None
    created_at: dt.datetime


class ThreadOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    someday: SomedayOut | None
    plan_id: uuid.UUID | None
    group_id: uuid.UUID | None
    participants: list[HolderOut]
    last_message: MessageOut | None
    unread: bool
    created_at: dt.datetime


class ThreadDetailOut(ThreadOut):
    messages: list[MessageOut]


# ----------------------------------------------------------------------- plans


class PlaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    area: str
    provider: str = "google"
    # Absent unless something has had a reason to resolve them; Autocomplete
    # alone does not carry coordinates. See the Place model.
    lat: float | None = None
    lon: float | None = None


class PlanCreate(BaseModel):
    someday_id: uuid.UUID
    date: dt.date
    time_slot: str | None = None
    place: str | None = None
    place_id: uuid.UUID | None = None
    people: list[uuid.UUID] = []
    split: str = "kkb"


class PlanPatch(BaseModel):
    date: dt.date | None = None
    time_slot: str | None = None
    place: str | None = None
    place_id: uuid.UUID | None = None
    split: str | None = None
    people: list[uuid.UUID] | None = None


class PlanOut(BaseModel):
    id: uuid.UUID
    someday: SomedayOut
    date: dt.date
    time_slot: str | None
    place: str | None
    place_id: uuid.UUID | None
    split: str
    people: list[HolderOut]
    confirmed_ids: list[uuid.UUID]
    done_at: dt.datetime | None
    thread_id: uuid.UUID | None
    created_at: dt.datetime


# --------------------------------------------------------------------- journal


class JournalIn(BaseModel):
    body: str = Field(default="", max_length=4000)
    photo_key: str | None = None


class JournalOut(BaseModel):
    id: uuid.UUID
    someday_id: uuid.UUID
    body: str
    photo_url: str | None
    created_at: dt.datetime


# ----------------------------------------------------------------------- media


class PresignUploadIn(BaseModel):
    filename: str
    content_type: str


class PresignUploadOut(BaseModel):
    key: str
    upload_url: str


class ClaimPhotoIn(BaseModel):
    """Only the key comes back.

    This used to reuse PresignUploadOut, which made `upload_url` a required
    field on the way in - so a client that sensibly sent just the key got a 422.
    An output shape is not an input shape.
    """

    key: str


# ---------------------------------------------------------------------- people


class PersonOut(BaseModel):
    id: uuid.UUID
    name: str
    handle: str
    gender: str | None
    city: str | None
    bio: str
    work: str
    education: str
    interests: list[str]
    hometown: str | None
    languages: list[str]
    created_at: dt.datetime
    photos: list[PhotoOut]
    somedays: list[SomedayOut]
    in_common: list[SomedayOut]
    # Things they have actually done. The journal stays private - the words and
    # the photograph are theirs - but that a plan happened is the one piece of
    # evidence this app has that any of it is real, and hiding it entirely left
    # a profile that could only ever promise.
    record: list[SomedayOut]
    is_friend: bool
    request_pending: bool


class ReportIn(BaseModel):
    reason: str = Field(min_length=1, max_length=60)
