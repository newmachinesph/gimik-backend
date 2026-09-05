"""The deck: what it shows, in what order, and what it refuses to show.

These are behavioural rather than numerical. Asserting that a particular card
lands in slot three would pin the weights in place and break on every tuning
pass; asserting that a person who keeps hiking sees more hiking than someone who
passes it is the property the weights exist to produce, and it survives them
being retuned. The exceptions are the privacy tests, which assert exact
membership - "does not appear" is the whole point of those.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.models import Friendship, Keep, Pass, Someday
from app.security import decode_token
from app.services import feed as feed_service

pytestmark = pytest.mark.anyio


def user_id_of(headers: dict[str, str]) -> uuid.UUID:
    return uuid.UUID(decode_token(headers["Authorization"].split(" ", 1)[1], expect="access")["sub"])


async def make_catalog(client, spec: list[tuple[str, str, str]]) -> dict[str, uuid.UUID]:
    """`spec` is (slug, category, domain); titles are derived from the slug."""
    ids: dict[str, uuid.UUID] = {}
    async with client.sessionmaker() as session:
        for slug, category, domain in spec:
            row = Someday(
                slug=slug,
                title=slug.replace("-", " ").capitalize(),
                category=category,
                domain=domain,
                party="any",
            )
            session.add(row)
            await session.flush()
            ids[slug] = row.id
        await session.commit()
    return ids


async def fetch(client, headers, **params) -> dict:
    response = await client.get("/feed", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def categories(page: dict) -> list[str]:
    return [item["someday"]["category"] for item in page["items"]]


def slugs(page: dict) -> list[str]:
    return [item["someday"]["slug"] for item in page["items"]]


def titles(page: dict) -> list[str]:
    return [item["someday"]["title"] for item in page["items"]]


# --------------------------------------------------------------- the basics


async def test_deck_excludes_what_you_have_already_decided(client, make_user):
    ids = await make_catalog(
        client,
        [
            ("a", "Food & Drink", "Cafes"),
            ("b", "Food & Drink", "Cafes"),
            ("c", "Water & Islands", "Reefs"),
        ],
    )
    headers = await make_user("Deciding")

    kept = await client.put(
        f"/somedays/{ids['a']}/keep",
        json={"open_to": ["friends"], "party_as": ["duo"]},
        headers=headers,
    )
    assert kept.status_code == 201, kept.text
    passed = await client.put(f"/somedays/{ids['b']}/pass", headers=headers)
    assert passed.status_code == 204, passed.text

    page = await fetch(client, headers, limit=10)
    assert slugs(page) == ["c"], "a keep and a pass are both decisions; neither comes back"


async def test_deck_reports_exhaustion_rather_than_repeating(client, make_user):
    ids = await make_catalog(client, [("only", "Food & Drink", "Cafes")])
    headers = await make_user("Completionist")
    await client.put(f"/somedays/{ids['only']}/pass", headers=headers)

    page = await fetch(client, headers, limit=10)
    assert page["items"] == []
    assert page["exhausted"] is True, "an empty deck says so instead of reshuffling passes"


async def test_a_fresh_account_is_shown_a_spread_not_one_shelf(client, make_user):
    """Cold start has no taste to exploit, so it should sample broadly."""
    spec = []
    for category in ("Food & Drink", "Water & Islands", "Culture & Heritage", "City & Social"):
        for index in range(10):
            spec.append((f"{category[:4].lower()}-{index}", category, category + " domain"))
    await make_catalog(client, spec)
    headers = await make_user("Newcomer")

    page = await fetch(client, headers, limit=8)
    assert len(set(categories(page))) >= 4, (
        "with no signal the deck should cover the catalog, not guess one shelf"
    )


async def test_one_category_cannot_take_over_a_page(client, make_user):
    """Even when a category is most of the catalog, it must not be most of a page."""
    spec = [(f"food-{i}", "Food & Drink", "Cafes") for i in range(40)]
    spec += [(f"water-{i}", "Water & Islands", "Reefs") for i in range(4)]
    spec += [(f"city-{i}", "City & Social", "Markets") for i in range(4)]
    await make_catalog(client, spec)
    headers = await make_user("Browser")

    page = await fetch(client, headers, limit=9)
    shown = categories(page)
    counts = {name: shown.count(name) for name in set(shown)}
    assert max(counts.values()) <= 6, f"one category swamped the page: {counts}"


# ----------------------------------------------------------------- learning


async def test_the_deck_leans_toward_what_you_keep(client, make_user):
    """Keep food, pass water, and the next page should reflect that."""
    spec = [(f"food-{i}", "Food & Drink", "Cafes") for i in range(20)]
    spec += [(f"water-{i}", "Water & Islands", "Reefs") for i in range(20)]
    ids = await make_catalog(client, spec)
    headers = await make_user("Learner")

    for index in range(6):
        await client.put(
            f"/somedays/{ids['food-' + str(index)]}/keep",
            json={"open_to": ["friends"], "party_as": ["duo"]},
            headers=headers,
        )
        await client.put(f"/somedays/{ids['water-' + str(index)]}/pass", headers=headers)

    shown = categories(await fetch(client, headers, limit=10))
    assert shown.count("Food & Drink") > shown.count("Water & Islands"), (
        "six keeps against six passes did not move the ranking: " + str(shown)
    )


async def test_a_friends_keep_lifts_a_card_and_is_explained(client, make_user):
    ids = await make_catalog(
        client, [(f"c-{i}", "Culture & Heritage", "Museums") for i in range(12)]
    )
    me = await make_user("Viewer")
    them = await make_user("Friend")
    my_id, their_id = user_id_of(me), user_id_of(them)

    async with client.sessionmaker() as session:
        session.add(Friendship(requester_id=my_id, addressee_id=their_id, status="accepted"))
        session.add(
            Keep(user_id=their_id, someday_id=ids["c-7"], open_to=["friends"], party_as=["duo"])
        )
        await session.commit()

    page = await fetch(client, me, limit=6)
    entry = next((item for item in page["items"] if item["someday"]["slug"] == "c-7"), None)
    assert entry is not None, "a card a friend kept should reach the first page of twelve"
    assert "friends" in entry["reasons"], "and the card should be able to say why"


# ------------------------------------------------------------------ privacy


async def test_someone_elses_private_someday_never_appears(client, make_user):
    """A custom someday with no keep behind it is a sentence in a private list."""
    me = await make_user("Stranger", city="Manila")
    them = await make_user("Author", city="Manila")
    async with client.sessionmaker() as session:
        session.add(
            Someday(
                slug=None,
                title="A secret plan",
                category="City & Social",
                owner_id=user_id_of(them),
            )
        )
        await session.commit()

    assert "A secret plan" not in titles(await fetch(client, me, limit=20))


async def test_a_custom_someday_reaches_the_deck_only_when_opened_wide_enough(client, make_user):
    """The author's own visibility choice decides, exactly as it does elsewhere.

    Two viewers, one author, one someday opened only to friends: the friend sees
    it, the stranger in the same city does not.
    """
    author = await make_user("Author", city="Manila")
    friend = await make_user("Friend", city="Manila")
    stranger = await make_user("Stranger", city="Manila")
    author_id, friend_id = user_id_of(author), user_id_of(friend)

    async with client.sessionmaker() as session:
        someday = Someday(
            slug=None, title="Rooftop supper club", category="City & Social", owner_id=author_id
        )
        session.add(someday)
        await session.flush()
        session.add(
            Keep(
                user_id=author_id,
                someday_id=someday.id,
                open_to=["friends"],
                party_as=["group"],
            )
        )
        session.add(Friendship(requester_id=author_id, addressee_id=friend_id, status="accepted"))
        await session.commit()

    assert "Rooftop supper club" in titles(await fetch(client, friend, limit=20))
    assert "Rooftop supper club" not in titles(await fetch(client, stranger, limit=20)), (
        "open_to=friends must not leak to a stranger who merely shares a city"
    )


# --------------------------------------------------------------- second look


async def test_an_old_pass_comes_back_but_a_recent_one_does_not(client, make_user):
    """Taste moves. A pass from last month is a question; one from today is an answer."""
    ids = await make_catalog(
        client, [("stale", "Food & Drink", "Cafes"), ("fresh", "Food & Drink", "Cafes")]
    )
    headers = await make_user("Reconsidering")
    me = user_id_of(headers)

    async with client.sessionmaker() as session:
        long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=feed_service.SECOND_LOOK_DAYS + 5)
        session.add(Pass(user_id=me, someday_id=ids["stale"], created_at=long_ago))
        session.add(Pass(user_id=me, someday_id=ids["fresh"]))
        await session.commit()

    page = await fetch(client, headers, limit=10)
    assert slugs(page) == ["stale"], "only the stale pass is old enough to ask again"
    assert "second_look" in page["items"][0]["reasons"], "and the card should admit it is a repeat"
    assert page["exhausted"] is False


async def test_a_keep_is_never_resurfaced(client, make_user):
    """Unlike a pass, a keep is already on the list - asking again is a bug."""
    ids = await make_catalog(client, [("kept", "Food & Drink", "Cafes")])
    headers = await make_user("Keeper")
    me = user_id_of(headers)

    async with client.sessionmaker() as session:
        session.add(
            Keep(
                user_id=me,
                someday_id=ids["kept"],
                open_to=["friends"],
                party_as=["duo"],
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=365),
            )
        )
        await session.commit()

    page = await fetch(client, headers, limit=10)
    assert page["items"] == []
    assert page["exhausted"] is True


# ----------------------------------------------------------------- filtering


async def test_the_category_filter_is_honoured(client, make_user):
    await make_catalog(
        client,
        [
            ("f1", "Food & Drink", "Cafes"),
            ("f2", "Food & Drink", "Cafes"),
            ("w1", "Water & Islands", "Reefs"),
        ],
    )
    headers = await make_user("Filtering")
    page = await fetch(client, headers, limit=10, category="Food & Drink")
    assert set(categories(page)) == {"Food & Drink"}


async def test_the_order_is_stable_within_a_day(client, make_user):
    """Two identical calls agree, so a refill does not reshuffle the deck underfoot."""
    await make_catalog(client, [(f"s-{i}", "Food & Drink", "Cafes") for i in range(30)])
    headers = await make_user("Repeating")
    first = await fetch(client, headers, limit=10)
    second = await fetch(client, headers, limit=10)
    assert slugs(first) == slugs(second)


async def test_two_people_do_not_get_the_same_deck(client, make_user):
    """Same empty history, same catalog - the exploration seed still differs."""
    await make_catalog(client, [(f"x-{i}", "Food & Drink", "Domain " + str(i % 4)) for i in range(60)])
    first = await fetch(client, await make_user("One"), limit=12)
    second = await fetch(client, await make_user("Two"), limit=12)
    assert slugs(first) != slugs(second), "identical decks for every new account is a bug"


# ------------------------------------------------------------ unit-level maths


def test_taste_starts_neutral_and_moves_with_evidence():
    """The smoothing is the point: one pass is noise, ten are a preference."""
    shelf = ("Food & Drink", "Cafes")

    cold = feed_service.Taste({}, {}, {}, {}, base_rate=0.5, interest_tokens=set())
    assert cold.score(*shelf) == pytest.approx(0.5)

    barely = feed_service.Taste(
        {}, {"Food & Drink": 1}, {}, {"Cafes": 1}, base_rate=0.5, interest_tokens=set()
    )
    convinced = feed_service.Taste(
        {}, {"Food & Drink": 20}, {}, {"Cafes": 20}, base_rate=0.5, interest_tokens=set()
    )
    assert barely.score(*shelf) > convinced.score(*shelf), "evidence should accumulate"
    assert barely.score(*shelf) > 0.35, "a single pass must not collapse a category"
    assert convinced.score(*shelf) < 0.2, "twenty passes should"


def test_diversify_breaks_up_a_run_of_one_category():
    """The greedy pass is what stops nine hiking cards in a row."""

    def card(slug: str, category: str, score: float) -> feed_service.Candidate:
        return feed_service.Candidate(
            someday_id=uuid.uuid4(),
            title=slug,
            category=category,
            domain=category,
            score=score,
        )

    # Eight strong food cards and two weaker others: sorting alone returns all food.
    candidates = [card("food-" + str(i), "Food", 1.0 - i * 0.01) for i in range(8)]
    candidates += [card("water", "Water", 0.80), card("city", "City", 0.79)]

    picked = feed_service.diversify(candidates, limit=4)
    assert len({c.category for c in picked}) >= 2, (
        "a diversity pass that returns one category is not doing anything"
    )


# ------------------------------------------------- somedays people write


async def test_writing_your_own_someday_keeps_it_and_leaves_it_off_the_catalog(client, make_user):
    """A custom someday is yours, wanted, and not editorial content."""
    headers = await make_user("Author")
    response = await client.post(
        "/somedays",
        json={
            "title": "Drive to my lola's town and find her bakery",
            "category": "City & Social",
            "open_to": ["friends"],
            "party_as": ["duo"],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    made = response.json()
    assert made["someday"]["slug"] is None, "a custom someday has no slug"
    assert made["someday"]["artwork_url"] is None, "and therefore no artwork"
    assert made["open_to"] == ["friends"]

    mine = (await client.get("/me/somedays", headers=headers)).json()
    assert any(k["someday"]["id"] == made["someday"]["id"] for k in mine), (
        "writing it down is wanting it - the author should not have to swipe on it"
    )

    listing = (await client.get("/catalog", params={"q": "lola"})).json()
    assert listing["total"] == 0, "someone's own sentence is not part of the browsable catalog"


async def test_retyping_the_same_someday_does_not_make_a_second_one(client, make_user):
    """Two identical rows would split their own holders between them."""
    headers = await make_user("Repeater")
    body = {
        "title": "Learn to bake pandesal",
        "category": "Food & Drink",
        "open_to": ["friends"],
        "party_as": ["duo"],
    }
    first = (await client.post("/somedays", json=body, headers=headers)).json()
    second = (await client.post("/somedays", json=body, headers=headers)).json()
    assert first["someday"]["id"] == second["someday"]["id"]


async def test_a_someday_cannot_be_filed_under_an_invented_category(client, make_user):
    headers = await make_user("Miscategorising")
    response = await client.post(
        "/somedays",
        json={"title": "Something entirely new", "category": "Interpretive Dance", "open_to": [], "party_as": []},
        headers=headers,
    )
    assert response.status_code == 422


async def test_an_older_category_name_is_accepted_and_stored_canonically(client, make_user):
    """A client mid-update still says "Food"; it means Food & Drink."""
    headers = await make_user("Legacy")
    response = await client.post(
        "/somedays",
        json={"title": "Find the best lugaw in town", "category": "Food", "open_to": [], "party_as": []},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["someday"]["category"] == "Food & Drink"


async def test_a_friends_someday_does_not_wait_for_the_catalog_to_run_out(client, make_user):
    """The whole point of letting people write their own.

    This shipped broken: the community pool was drawn only once the catalog pool
    fell below one page, and the catalog is 2,522 concepts - so a friend's
    someday was reachable in principle and unreachable in practice. The catalog
    here is deliberately far larger than the page being asked for.
    """
    await make_catalog(client, [(f"c-{i}", "Food & Drink", "Cafes") for i in range(60)])
    author = await make_user("Author", city="Manila")
    viewer = await make_user("Viewer", city="Manila")
    author_id, viewer_id = user_id_of(author), user_id_of(viewer)

    async with client.sessionmaker() as session:
        session.add(Friendship(requester_id=author_id, addressee_id=viewer_id, status="accepted"))
        await session.commit()

    made = await client.post(
        "/somedays",
        json={
            "title": "Drive to my lola's town and find her bakery",
            "category": "City & Social",
            "open_to": ["friends"],
            "party_as": ["duo"],
        },
        headers=author,
    )
    assert made.status_code == 201, made.text

    page = await fetch(client, viewer, limit=10)
    assert "Drive to my lola's town and find her bakery" in titles(page), (
        "a friend's someday must compete with the catalog, not queue behind all of it"
    )


async def test_a_strangers_someday_still_waits_for_the_catalog(client, make_user):
    """Same city is not the same as knowing someone.

    Opening a someday to everyone should reach the neighbourhood eventually. It
    should not jump the catalog to do it, or the deck becomes whatever strangers
    typed today.
    """
    await make_catalog(client, [(f"c-{i}", "Food & Drink", "Cafes") for i in range(60)])
    stranger = await make_user("Stranger", city="Manila")
    viewer = await make_user("Viewer", city="Manila")

    made = await client.post(
        "/somedays",
        json={
            "title": "Queue at the new lugaw place",
            "category": "Food & Drink",
            "open_to": ["everyone"],
            "party_as": ["duo"],
        },
        headers=stranger,
    )
    assert made.status_code == 201, made.text

    page = await fetch(client, viewer, limit=10)
    assert "Queue at the new lugaw place" not in titles(page)
