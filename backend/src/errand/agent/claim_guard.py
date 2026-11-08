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
`guard.claim_rewritten`. Negation exempts a claim only where it scopes
over the claim verb ("not booked yet", "once you approve it will be booked"); "booked, no
worries" is still a claim.
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
# Negation exempts a claim only when it scopes over the claim verb (10.8): a negator in
# the few words just before the verb within the same clause ("not booked", "haven't
# booked", "nothing has been booked"), a negator right after it (Hinglish "ho gaya
# nahi"), a conditional opener before it in the same clause ("once you approve it is
# booked"), or a future or modal frame ("will be booked"). A negator anywhere else in
# the sentence ("booked, no worries", "booked until further notice") exempts nothing.
_NEGATOR = re.compile(
    r"\b(not|no|never|nothing|none|yet to|haven'?t|hasn'?t|hadn'?t|isn'?t|aren'?t|wasn'?t|"
    r"weren'?t|can'?t|cannot|won'?t|don'?t|didn'?t|nahi|nahin)\b|n't\b",
    re.I,
)
# Phrases that carry a negator word but negate nothing.
_IDIOMS = re.compile(
    r"\b(?:no (?:worries|problem|doubt|need|hurry|rush|charge|extra)|not to worry|"
    r"nothing else|not only|no more|not just|no longer)\b",
    re.I,
)
_CLAUSE_BREAK = re.compile(r"[,;:()\u2013\u2014]|\s-\s|\b(?:but|and|so|then|though)\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:once|after|when|if|unless|until) (?:you|i|it|the|they|we)\b", re.I)
# A condition that is the user's own approval ("once you tap approve it is booked").
_APPROVAL_CONDITION = re.compile(
    r"^(?:once|after|when|if) (?:you|the user) (?:tap|approve|confirm|accept|grant|press|click|"
    r"say yes|say ok|okay|give (?:the )?(?:go ?ahead|approval))",
    re.I,
)
_MODAL = re.compile(r"\b(?:will|would|can|could|should|shall|may|might|must)\b|'ll\b", re.I)
_FUTURE = re.compile(
    r"(?:\b(?:will|would|can|could|should|shall|to|going to)\s+(?:also\s+)?be\s+|'ll\s+be\s+|"
    r"\b(?:will|would|shall)\s+|'ll\s+)$",
    re.I,
)
# The only words that may sit between a negator and the claim verb for the negator to
# scope over it: auxiliaries, "yet", subject pronouns, booking nouns, determiners.
# "don't worry I booked" and "no stress it's booked" fail this and stay claims.
_BRIDGE = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "am", "has",
    "have", "had", "having", "do", "does", "did", "get", "gets", "got",
    "yet", "still", "ever", "even", "actually", "currently", "really", "fully", "properly",
    "also", "i", "you", "we", "they", "he", "she", "it", "that",
    "this", "there", "it's", "i've", "we've", "you've", "they've", "that's", "there's",
    "a", "an", "the", "any", "your", "my", "their", "its", "one",
    "anything", "everything", "something", "booking", "bookings", "appointment",
    "appointments", "slot", "plumber",
    "electrician", "job", "visit", "errand", "request", "order", "service", "to",
})  # fmt: skip
_TRAILING_NO = re.compile(r"^\s+(?:hai\s+|hua\s+|hui\s+)?(?:nahi|nahin)\b", re.I)
# Words that make "done" or "sorted" something other than "the errand is finished".
_DONE_OBJECT = re.compile(
    r"^\s+(?:a|an|the|some|my|this|that|one)\s+(?!book|reserv|appointment|job)", re.I
)
_SORTED_BY = re.compile(
    r"^\s+(?:by|in|from|on|according|based|alphabetically|with|so|using)\b", re.I
)
BOOKING_ID = re.compile(r"\bB-\d+\b")
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


def _negated_before(clause: str) -> bool:
    """True when the nearest negator before the claim reaches it through bridge words only."""
    text = re.sub(r"\byet to\b", "not", _IDIOMS.sub(" ", clause), flags=re.I)
    for word in reversed(re.findall(r"[\w']+", text.lower())):
        if _NEGATOR.search(word):
            return True
        if word not in _BRIDGE:
            return False
    return False


def _scoped_negation(sentence: str, match: re.Match[str]) -> bool:
    """True when a negation, condition or future frame scopes over this claim match."""
    before = sentence[: match.start()]
    if _negated_before(_CLAUSE_BREAK.split(before)[-1]):
        return True
    if _TRAILING_NO.match(sentence[match.end() :]):
        return True
    if _FUTURE.search(before):
        return True
    cond = [c for c in _CONDITIONAL.finditer(before)]
    if cond:
        # A condition exempts only a future or modal claim, or one conditioned on the
        # user's own approval; "when you asked I booked it" is a past-tense claim.
        after_opener = before[cond[-1].start() :]
        if _APPROVAL_CONDITION.match(after_opener) or _MODAL.search(after_opener):
            return True
        return bool(re.match(r"(?:will|'ll)\b", match.group(0), re.I))
    return False


def _real_claim(sentence: str, match: re.Match[str]) -> bool:
    word = match.group(0).lower()
    rest = sentence[match.end() :]
    if word == "done" and _DONE_OBJECT.match(rest):
        return False
    if word == "sorted" and _SORTED_BY.match(rest):
        return False
    return not _scoped_negation(sentence, match)


def _verb(sentence: str) -> Kind | None:
    """The kind of record the sentence claims, if any claim in it is not negated."""
    for kind, rx in (("cancel", _CANCEL), ("reschedule", _RESCHEDULE), ("book", _BOOK)):
        if any(_real_claim(sentence, m) for m in rx.finditer(sentence)):
            return kind  # type: ignore[return-value]
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
    ids = set(BOOKING_ID.findall(sentence))
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
        if kind is None or _backed(sentence, kind, records, names, tz):
            kept.append(sentence.strip())
        else:
            removed.append(sentence.strip())
    return Guarded(" ".join(kept), removed)
