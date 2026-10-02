"""The objectionable-content filter every piece of user-written text passes.

App Store guideline 1.2 asks for three things of an app with user-generated
content: a filter, a way to flag, and a way to block. Flagging and blocking live
in `app.routers.social`; this is the filter.

Two tiers, because a public sentence and a private one are different things. A
someday title, a profile and a group name are read by strangers, so they are
held to the stricter list. A message between people who chose to talk is only
refused for the words that are abuse in any company.

The lists hold only words with no ordinary meaning. A filter that refuses
"Maine Coon", "blue tits", "spic and span" or a man called Dick teaches people
it is broken, so anything with an innocent reading is left to reports and
blocks, which have a person behind them.
"""

from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException

# Shown to the person who wrote it. The app shows a 422's detail as it is,
# which is better than telling someone their message "did not save".
REJECTED = "That contains language Gimik does not allow."

# Slurs, sexual violence and threats: refused everywhere, messages included.
_SEVERE = frozenset(
    """
    nigger nigga niggers niggas faggot faggots tranny trannies retard retards
    retarded kike kikes gook gooks wetback wetbacks rape rapes raped raping
    rapist rapists molest molested pedo pedos pedophile pedophiles childporn
    kys
    """.split()
)

# Profanity and sexual language: refused wherever strangers can read it.
_PROFANE = frozenset(
    """
    fuck fucks fucked fucker fuckers fucking motherfucker motherfuckers shit
    shits shitty bullshit bitch bitches asshole assholes cunt cunts pussy
    pussies whore whores slut sluts porn porno blowjob handjob jerkoff cumshot
    nudes boobs titties horny sext sexting
    puta putang putangina tangina tanginamo punyeta pakyu gago tarantado
    tarantada ulol kupal kantot kantutan jakol pekpek burat pokpok malibog
    libog
    """.split()
)

# Stems that are abuse inside any word, so "fuckyou" and "tanginamo" do not
# slip through for want of a space. Nothing ordinary contains either.
_STEMS = ("fuck", "tangina")

# ponytail: a word blocklist. It misses spelled-out abuse ("f.u.c.k"), new
# coinages and anything in a photo; reports and blocks are what catch those.
# Swap in a hosted moderation model when volume makes hand-review slow.
_LEET = str.maketrans("013457@$", "oieastas")
_WORD = re.compile(r"[a-z]+")
_RUN = re.compile(r"(.)\1{2,}")
_INVISIBLE = re.compile(r"[​-‏⁠﻿]")


def _words(text: str) -> list[str]:
    """Lowercase words with the cheap disguises undone: accents, zero-width
    characters, digits standing in for letters, and letters held down."""
    plain = unicodedata.normalize("NFKD", _INVISIBLE.sub("", text))
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return _WORD.findall(_RUN.sub(r"\1", plain.lower().translate(_LEET)))


def objectionable(text: str | None, *, public: bool = True) -> bool:
    """True when `text` holds a word on the list for that audience."""
    if not text:
        return False
    blocked = _SEVERE | _PROFANE if public else _SEVERE
    return any(
        word in blocked or (public and any(stem in word for stem in _STEMS))
        for word in _words(text)
    )


def screen(*texts: str | None, public: bool = True) -> None:
    """Refuse the write, saying why, if any of the texts is objectionable."""
    if any(objectionable(text, public=public) for text in texts):
        raise HTTPException(status_code=422, detail=REJECTED)
