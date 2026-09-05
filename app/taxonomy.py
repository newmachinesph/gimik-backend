"""One vocabulary for the catalog: nine categories, and the aliases they absorb.

The catalog arrived in two waves. The first 522 concepts were written against a
one-word taxonomy (Food, Nature, Skills, Fitness); the 2,000-activity export
that followed uses a broader nine (Food & Drink, Nature & Countryside, Travel &
Learning, Wellness & Sports). Left alone that is eighteen filter chips over one
catalog, half of them synonyms of the other half - and a person filtering by
"Food" would be shown a third of the food.

So the export's nine are canonical and the older names are aliases of them. The
mapping lives here rather than in either seeder because both write categories
and the feed reads them: three callers, one definition. `canonical()` is applied
on the way in, so the column only ever holds a canonical value and nothing
downstream has to know this file exists.

Adding a category is a product decision, not a data one - a new name that is not
an alias will pass through `canonical()` untouched and appear as a tenth chip,
which is loud enough to notice in review.
"""

from __future__ import annotations

# The nine, in the order the app shows them. Order is editorial: the broad,
# high-volume shelves first, the deliberately scarce one last.
CATEGORIES: tuple[str, ...] = (
    "Adventure & Outdoors",
    "Nature & Countryside",
    "Water & Islands",
    "Food & Drink",
    "Culture & Heritage",
    "City & Social",
    "Wellness & Sports",
    "Travel & Learning",
    "Signature & Rare",
)

# Older one-word names -> the shelf they belong on. "Community" and "Cities"
# both land on City & Social: the distinction they drew (people you meet vs
# places you go) is not one anybody browsing was asked to care about.
ALIASES: dict[str, str] = {
    "Adventure": "Adventure & Outdoors",
    "Nature": "Nature & Countryside",
    "Water": "Water & Islands",
    "Food": "Food & Drink",
    "Culture": "Culture & Heritage",
    "Community": "City & Social",
    "Cities": "City & Social",
    "Fitness": "Wellness & Sports",
    "Skills": "Travel & Learning",
}


def canonical(category: str | None) -> str | None:
    """The canonical name for a category, or the input unchanged.

    Unknown names pass through rather than raising: a seeder is not the place to
    discover a typo by crashing halfway through a catalog, and an unmapped value
    shows up immediately as an unexpected chip.
    """
    if category is None:
        return None
    name = category.strip()
    return ALIASES.get(name, name)


def is_canonical(category: str) -> bool:
    return category in CATEGORIES
