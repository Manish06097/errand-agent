"""The claim guard: model text may not say something happened that no record shows (10.8).

Structural first: `respond` puts a code status line under any model text whenever the
errand is not completed, so a paraphrased claim is never the only signal on screen.
This module is the lexical second layer. It finds claim sentences, by a widened
lexicon that includes paraphrases ("all set", "locked in", "sorted", "will come at")
and Hinglish ("ho gaya", "kar diya", "pakka"), and checks each against the records by
verb and object:

- the verb decides the kind of record that can back it (a booking claim needs a
  verified booking, a cancel claim a verified cancellation);
- every object the sentence names (a booking id, a provider, a service, a weekday)
  must agree with one such record;
- a claim that names nothing ("All done!") is backed only by an outcome verified in
  this run.

Records are this run's verified outcomes plus `known_bookings`, which holds a booking
only after `get_booking_status` confirmed it. "Your booking B-5521 is confirmed" passes
in a later run when B-5521 is confirmed; "I've booked the electrician too" fails when
only a plumber was. A sentence that fails is removed and `respond` emits
`guard.claim_rewritten`. Negated and conditional sentences ("not booked yet", "once
you approve it will be booked") are not claims.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Literal

Kind = Literal["book", "cancel", "reschedule"]

_BOOK = re.compile(
    r"\b(booked|book ho gaya|book kar diya|book kar di|reserved|secured|confirmed|"
    r"scheduled|arranged|locked in|all set|you'?re set|you are set|good to go|done|sorted|"
    r"taken care of|ho gaya|ho gayi|kar diya|kar di|pakka|confirm ho gaya|"
    r"(?:will|'ll) (?:come|arrive|be there|visit|be at)|is coming|are coming|"
    r"on (?:the|his|her) way|see you)\b",
    re.I,
)
_CANCEL = re.compile(r"\b(cancelled|canceled|called off|cancel ho gaya|cancel kar diya)\b", re.I)
_RESCHEDULE = re.compile(r"\b(rescheduled|moved|shifted|reschedule ho gaya)\b", re.I)
_NEGATED = re.compile(
    r"\b(not|no|never|nothing|none|yet to|until|unless|once you|after you|when you|if you|"
    r"haven'?t|hasn'?t|isn'?t|aren'?t|wasn'?t|can'?t|cannot|won'?t|don'?t|nahi|nahin)\b|n't\b",
    re.I,
)
_BOOKING_ID = re.compile(r"\bB-\d+\b")
_SERVICES = {
    "plumber": re.compile(r"\b(plumber|plumbing)\b", re.I),
    "electrician": re.compile(r"\b(electrician|electrical|electric)\b", re.I),
    "ac_servicing": re.compile(r"\b(ac|a/c|air ?con\w*|air conditioner)\b", re.I),
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:s?day|nesday|rsday|urday)?\b", re.I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class Verified:
    """A record that can back a claim."""

    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    slot_start: datetime
    kind: Kind = "book"
    this_run: bool = False


@dataclass
class Guarded:
    text: str
    removed: list[str] = field(default_factory=list)


def _verb(sentence: str) -> Kind | None:
    if _CANCEL.search(sentence):
        return "cancel"
    if _RESCHEDULE.search(sentence):
        return "reschedule"
    if _BOOK.search(sentence):
        return "book"
    return None


def _weekday(token: str) -> int:
    t = token.lower()[:3]
    return next(i for i, name in enumerate(_WEEKDAYS) if name.startswith(t))


def _name_words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z]{3,}", name)][:1]


def _backed(
    sentence: str,
    kind: Kind,
    records: Sequence[Verified],
    names: dict[str, str],
    tz: tzinfo,
) -> bool:
    candidates = [r for r in records if r.kind == kind]
    ids = set(_BOOKING_ID.findall(sentence))
    services = {s for s, rx in _SERVICES.items() if rx.search(sentence)}
    days = {_weekday(m.group(1)) for m in _WEEKDAY.finditer(sentence)}
    lowered = sentence.lower()
    providers = {
        pid
        for pid, name in names.items()
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in _name_words(name))
    }
    if not (ids or services or days or providers):
        return any(r.this_run for r in candidates)
    for r in candidates:
        if ids and ids != {r.booking_id}:
            continue
        if services and services != {r.service}:
            continue
        if days and days != {r.slot_start.astimezone(tz).weekday()}:
            continue
        if providers and r.provider_id not in providers:
            continue
        return True
    return False


def guard(
    text: str,
    records: Sequence[Verified],
    tz: tzinfo,
    *,
    provider_names: dict[str, str] | None = None,
) -> Guarded:
    """Remove every claim sentence no record backs. `provider_names` maps every provider
    the conversation has seen to its name, so naming an unbooked one is caught too."""
    names = dict(provider_names or {})
    for r in records:
        names.setdefault(r.provider_id, r.provider_name)
    kept: list[str] = []
    removed: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        if not sentence.strip():
            continue
        kind = _verb(sentence)
        if kind is None or _NEGATED.search(sentence) or _backed(sentence, kind, records, names, tz):
            kept.append(sentence.strip())
        else:
            removed.append(sentence.strip())
    return Guarded(" ".join(kept), removed)
