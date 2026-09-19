"""Catalog search: people type sentences, the catalog holds titles."""


async def test_a_sentence_finds_the_title_it_contains(client, somedays):
    """The create screen asks "what do you keep saying you should do?" - people
    answer "go dive a coral reef", not "Dive a coral reef". A whole-phrase
    substring match missed every one of those and they wrote duplicates."""
    listing = (await client.get("/catalog", params={"q": "go dive a coral reef"})).json()
    assert [item["title"] for item in listing["items"]] == ["Dive a coral reef"]


async def test_every_meaningful_word_must_match(client, somedays):
    listing = (await client.get("/catalog", params={"q": "coral aurora"})).json()
    assert listing["total"] == 0, "words narrow the search, they do not widen it"


async def test_word_order_does_not_matter(client, somedays):
    listing = (await client.get("/catalog", params={"q": "reef coral"})).json()
    assert listing["total"] == 1


async def test_a_single_word_still_works(client, somedays):
    listing = (await client.get("/catalog", params={"q": "aurora"})).json()
    assert [item["title"] for item in listing["items"]] == ["See an aurora"]


async def test_only_filler_words_fall_back_to_the_phrase(client, somedays):
    """A query of nothing but filler is not widened to the whole catalog."""
    listing = (await client.get("/catalog", params={"q": "to the"})).json()
    assert listing["total"] == 0
