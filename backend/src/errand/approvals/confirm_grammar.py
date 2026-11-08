"""The closed grammar for typed confirmations (architecture 8.3). A pure table.

A typed message can only ever be a grant if it is one of the action phrases for the
card's action, with an optional "yes" in front, an optional "please" before or after,
and trailing punctuation. Nothing is inferred: there is no model, no fuzzy matching, no
synonym list beyond this table.

    book:        book it, yes book it, confirm, confirm booking
    cancel:      cancel it, yes cancel it, confirm cancellation
    reschedule:  move it, yes move it, confirm

On top of the table, a grant text is at most 32 characters and contains no digit, no
time, no weekday, no negation and no "but", so "yes but at 11" or "book it for
tuesday" can never pass even if a phrase were added carelessly later.

Bare acknowledgements (`yes`, `ok`, `okay`, `yep`, `sure`, `haan`, `go ahead`, ...) are
recognised separately: they never grant; the messages route answers them with a hint
when the latest card allows typed grants.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Literal

Action = Literal["book", "cancel", "reschedule"]
MAX_CHARS = 32

PHRASES: dict[Action, frozenset[str]] = {
    "book": frozenset({"book it", "confirm", "confirm booking"}),
    "cancel": frozenset({"cancel it", "confirm cancellation"}),
    "reschedule": frozenset({"move it", "confirm"}),
}
# "yes book it" is in the table above as "book it" with the optional "yes"; the
# table in 8.3 lists both spellings, and both are accepted.
BARE_ACKS = frozenset(
    {
        "yes",
        "y",
        "ok",
        "okay",
        "k",
        "kk",
        "yep",
        "yeah",
        "yup",
        "sure",
        "fine",
        "alright",
        "all right",
        "go ahead",
        "do it",
        "haan",
        "han",
        "haa",
        "ha",
        "theek hai",
        "thik hai",
        "ji",
        "ok go ahead",
        "yes please",
        "ok please",
        "sounds good",
        "great",
    }
)

_DIGIT = re.compile(r"\d")
_TIME = re.compile(
    r"\b(am|pm|a\.m|p\.m|noon|midnight|o'?clock|morning|afternoon|evening|night|tonight|"
    r"today|tomorrow|tmrw|kal|aaj|hour|hours|minute|minutes|half|quarter)\b"
)
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|weekend)\b"
)
_NEGATION = re.compile(r"\b(no|not|never|dont|don't|nope|nah|nahi|mat|stop|wait)\b|n't\b")
_BUT = re.compile(r"\b(but|except|instead|unless|only if)\b")
_EDGE_PUNCT = re.compile(r"[\s.!,;:]+$")


class Typed(StrEnum):
    GRANT = "grant"  # an action phrase for this card's action
    ACK = "ack"  # a bare acknowledgement: never a grant
    OTHER = "other"  # an ordinary message


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = folded.replace("\u2019", "'")
    folded = re.sub(r"\s+", " ", folded).strip()
    return _EDGE_PUNCT.sub("", folded)


def forbidden_content(text: str) -> str | None:
    """Why a text can never be a grant, whatever the table says."""
    lowered = normalise(text)
    if len(text.strip()) > MAX_CHARS:
        return "too_long"
    if _DIGIT.search(lowered):
        return "digit"
    if _TIME.search(lowered):
        return "time"
    if _WEEKDAY.search(lowered):
        return "weekday"
    if _NEGATION.search(lowered):
        return "negation"
    if _BUT.search(lowered):
        return "but"
    return None


def _core(normalised: str) -> str:
    words = normalised.replace(",", " ").split()
    if words and words[0] == "please":
        words = words[1:]
    if words and words[-1] == "please":
        words = words[:-1]
    if words and words[0] == "yes":
        words = words[1:]
    if words and words[0] == "please":
        words = words[1:]
    return " ".join(words)


def classify(text: str, action: Action) -> Typed:
    if forbidden_content(text) is not None:
        return Typed.OTHER
    normalised = normalise(text)
    if _core(normalised) in PHRASES[action]:
        return Typed.GRANT
    if is_bare_ack(text):
        return Typed.ACK
    return Typed.OTHER


def is_bare_ack(text: str) -> bool:
    if forbidden_content(text) is not None:
        return False
    normalised = normalise(text)
    return normalised.replace(",", "") in BARE_ACKS or _core(normalised) in BARE_ACKS


def suggestion(action: Action) -> str:
    """The phrase the hint names: "book it", "cancel it", "move it"."""
    return {"book": "book it", "cancel": "cancel it", "reschedule": "move it"}[action]
