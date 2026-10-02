"""The objectionable-content filter every piece of user-written text passes.

App Store guideline 1.2 asks for three things of an app with user-generated
content: a filter, a way to flag, and a way to block. Flagging and blocking live
in `app.routers.social`; this is the filter.

Two tiers, because a public sentence and a private one are different things. A
someday title, a profile and a group name are read by strangers, so they are
held to the stricter list. A message between people who chose to talk is only
refused for the words that are abuse in any company.
"""

from __future__ import annotations

import re

from fastapi import HTTPException

# Shown to the person who wrote it. The app matches on the status and this
# exact text to say something better than "that did not save".
REJECTED = "That contains language Gimik does not allow."

# Slurs, sexual violence and threats: refused everywhere, messages included.
_SEVERE = frozenset(
    """
    nigger nigga niggers niggas faggot faggots fag fags tranny trannies retard
    retards retarded chink chinks spic spics kike kikes gook gooks wetback
    coon coons dyke dykes rape rapes raped raping rapist rapists molest
    molested pedo pedos pedophile pedophiles childporn kys
    """.split()
)

# Profanity and sexual language: refused wherever strangers can read it.
# Deliberately absent, because they are ordinary words here: "cum" (cum laude),
# "paki" (Tagalog for please), "cp" (a cellphone), "naked", "escort".
_PROFANE = frozenset(
    """
    fuck fucks fucked fucker fuckers fucking motherfucker motherfuckers shit
    shits shitty bullshit bitch bitches bastard bastards asshole assholes cunt
    cunts dick dicks cock cocks pussy pussies whore whores slut sluts porn
    porno blowjob handjob jerkoff cumshot anal nudes boobs tits titties horny
    hooker hookers sext sexting
    puta putang putangina tangina tanginamo punyeta pakyu gago tarantado
    tarantada ulol kupal kantot kantutan jakol tite titi pekpek burat pokpok
    malibog libog
    """.split()
)

# ponytail: an exact-word blocklist. It misses spaced-out or misspelled abuse
# and anything in a photo; reports and blocks are what catch those. Swap in a
# hosted moderation model when volume makes hand-review of reports slow.
_LEET = str.maketrans("013457@$", "oieastas")
_WORD = re.compile(r"[a-z]+")
_RUN = re.compile(r"(.)\1{2,}")


def objectionable(text: str | None, *, public: bool = True) -> bool:
    """True when any word of `text` is on the list for that audience."""
    if not text:
        return False
    blocked = _SEVERE | _PROFANE if public else _SEVERE
    # "f u u u c k" is not caught; "fuuuck" and "sh1t" are.
    normalized = _RUN.sub(r"\1", text.lower().translate(_LEET))
    return any(word in blocked for word in _WORD.findall(normalized))


def screen(*texts: str | None, public: bool = True) -> None:
    """Refuse the write, saying why, if any of the texts is objectionable."""
    if any(objectionable(text, public=public) for text in texts):
        raise HTTPException(status_code=422, detail=REJECTED)
