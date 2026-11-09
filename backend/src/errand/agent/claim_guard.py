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
# Negation exempts a claim only when it scopes over the claim verb (10.8): the nearest
# negator before the verb in the same clause, reaching it through a closed list of words
# that depends on the negator. "not", "n't" and "yet to" reach it through auxiliaries,
# pronouns and booking nouns ("haven't booked", "not yet confirmed"); "no" only when it
# determines the noun the claim is about ("no plumber is booked", never "No I booked it");
# "nothing" and "none" only as the subject of the claim verb, auxiliaries between
# ("nothing has been booked", never "Nothing to do it's booked"). A negator right after
# the verb (Hinglish "ho gaya nahi") or a future or modal frame ("will be booked") also
# exempts it. A condition reaches only the claim in its own clause (_condition_exempts).
# "Don't worry I booked", "no stress it's booked" and "when you asked I booked it" are
# claims; so is "booked, no worries".
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
# A condition that is the user's own approval in the present tense ("once you tap approve it
# is booked"). The \b after the verb keeps "approved", "tapped" and "confirmed" out: a
# grant that already happened says nothing about whether the booking did.
_APPROVAL_CONDITION = re.compile(
    r"^(?:once|after|when|if) (?:you|the user) "
    r"(?:taps?|approves?|confirms?|accepts?|grants?|presses|press|clicks?|"
    r"says? (?:yes|ok|okay)|okay|gives? (?:the )?(?:go ?ahead|approval))\b",
    re.I,
)
_HYPOTHETICAL = re.compile(r"^(?:if|unless)\b", re.I)
# A claim in the present tense ("it is booked", "it gets booked"), not "I booked it",
# "I've booked it" or "it has been booked".
_PRESENT_LEAD = frozenset({"is", "are", "am", "be", "being", "gets", "get", "it's", "that's",
                           "there's", "here's", "you're", "we're", "they're"})  # fmt: skip
_PRESENT_CLAIM = re.compile(r"^(?:is|are|you'?re|you are|all set|good to go|see you)\b", re.I)
_MODALS = frozenset({"will", "would", "can", "could", "should", "shall", "may", "might", "must"})
# What may sit between a modal's be/have/get and the claim verb for the modal to reach it:
# object pronouns, determiners, booking nouns, and the name of a provider on the shortlist
# or among the user's known bookings ("I can get Sharma booked", "I can have it booked").
# Never a subject pronoun, "is", a verb of its own ("you'll see it's booked", "you can tell
# me I booked it") or a capitalised word that is no known provider ("I can get Saturday
# booked").
_MODAL_LINK = frozenset({"be", "have", "get"})
_MODAL_OBJECT = frozenset({
    "been", "being", "be", "it", "you", "them", "that", "this", "a", "an", "the", "any", "your",
    "my", "their", "its", "one", "also", "anything", "everything", "something", "booking",
    "bookings", "appointment", "appointments", "slot", "plumber", "electrician", "job", "visit",
    "errand", "request", "order", "service",
})  # fmt: skip
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
    "have", "had", "having", "get", "gets", "got",
    "yet", "still", "ever", "even", "actually", "currently", "really", "fully", "properly",
    "also", "i", "you", "we", "they", "he", "she", "it", "that",
    "this", "there", "it's", "i've", "we've", "you've", "they've", "that's", "there's",
    "a", "an", "the", "any", "your", "my", "their", "its", "one",
    "anything", "everything", "something", "booking", "bookings", "appointment",
    "appointments", "slot", "plumber",
    "electrician", "job", "visit", "errand", "request", "order", "service",
})  # fmt: skip
# "nothing"/"none" reach the claim verb through auxiliaries and adverbs only.
_AUXILIARY = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "am", "has", "have", "had", "having",
    "get", "gets", "got", "yet", "still", "ever", "even", "actually", "currently", "really",
    "fully", "properly", "also",
})  # fmt: skip
# "no" scopes over the claim only when it determines one of these nouns.
_NO_NOUNS = frozenset({
    "booking", "bookings", "appointment", "appointments", "slot", "slots", "plumber",
    "plumbers", "electrician", "electricians", "provider", "providers", "job", "visit",
    "errand", "request", "order", "service", "confirmation", "one",
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
    # What was done to the booking: the verb a claim is matched against. A cancel or a
    # reschedule that `list_bookings` showed is a record of its own (N1, 10.8).
    kind: Kind = "book"
    this_run: bool = False
    # The intent that made the record, so a status line can tell this errand's own booking
    # from another one of the same service on the thread.
    source_intent_id: str | None = None


@dataclass
class Guarded:
    text: str
    removed: list[str] = field(default_factory=list)


_SUBJECTS = frozenset({"i", "we", "you", "they", "he", "she"})
# "you're all set", "we are booked", "it's booked": a subject pronoun and a be-form, the
# shape of a present claim under a present-tense approval (BUG-M4-2 round 5).
_SUBJECT_BE = re.compile(r"\b(?:(?:you|we|they)(?:'re|\s+are)|it(?:'s|\s+is))\b", re.I)


@dataclass(frozen=True)
class _Names:
    """The provider names a modal or an approval may bridge: those the caller passes (the
    shortlist and the user's known bookings), as whole names and as name words. Never a
    name read out of the model's own text."""

    whole: tuple[str, ...] = ()
    words: frozenset[str] = frozenset()

    @classmethod
    def of(cls, names: dict[str, str]) -> _Names:
        whole = sorted({n.strip() for n in names.values() if n.strip()}, key=len, reverse=True)
        words = {w for n in names.values() for w in _name_words(n)}
        return cls(tuple(whole), frozenset(words))

    def mask(self, text: str) -> str:
        """The text with each whole name replaced by "it", so a multi-word name is one
        object word and not several stranded ones."""
        if not self.whole:
            return text
        rx = "|".join(re.escape(n) for n in self.whole)
        return re.sub(rf"(?<![\w'])(?:{rx})(?![\w'])", " it ", text, flags=re.I)


def _negated_before(clause: str) -> bool:
    """True when the nearest negator before the claim reaches it by its own rule."""
    text = re.sub(r"\byet to\b", "not", _IDIOMS.sub(" ", clause), flags=re.I)
    text = re.sub(r"\bable to\b", "able", text, flags=re.I)  # "wasn't able to get it booked"
    words = re.findall(r"[\w']+", text.lower())
    for i in range(len(words) - 1, -1, -1):
        word = words[i]
        if not _NEGATOR.search(word):
            continue
        between = words[i + 1 :]
        base = word.removesuffix("'s")
        if base == "never" and len(between) >= 2 and between[1] in _SUBJECTS:
            return False  # inversion: "never have I booked" is a claim, not a negation
        if base in ("nothing", "none"):
            return all(w in _AUXILIARY for w in between)
        if base == "no":
            return (
                bool(between)
                and between[0] in _NO_NOUNS
                and all(w in _AUXILIARY for w in between[1:])
            )
        return all(w in _BRIDGE or w == "able" for w in between)
    return False


def _present_claim(clause_before: str, claim: str) -> bool:
    """True when the claim reads as present or future ("it is booked"), not past."""
    words = re.findall(r"[\w']+", clause_before.lower())
    while words and words[-1] in ("already", "now", "also", "fully", "all"):
        words.pop()
    return bool(words and words[-1] in _PRESENT_LEAD) or bool(_PRESENT_CLAIM.match(claim))


def _modal_reaches_claim(clause: str, names: _Names) -> bool:
    """True when the last modal in the clause reaches the claim verb through the closed
    list: modal, optional "also", then be/have/get and object words or a known provider
    name, then the claim. A capitalised word is not a name ("I can get Saturday booked")."""
    words = re.findall(r"[\w']+", names.mask(clause))
    last = [i for i, w in enumerate(words) if w.lower() in _MODALS or w.lower().endswith("'ll")]
    if not last:
        return False
    rest = words[last[-1] + 1 :]
    if rest and rest[0].lower() == "also":
        rest = rest[1:]
    if not rest:
        return True
    if rest[0].lower() not in _MODAL_LINK:
        return False
    return all(w.lower() in _MODAL_OBJECT or w.lower() in names.words for w in rest[1:])


_APPROVAL_OBJECT = frozenset({
    "approve", "approval", "confirm", "button", "yes", "ok", "okay", "go", "ahead", "goahead",
})  # fmt: skip


def _is_subject_word(word: str, names: _Names) -> bool:
    """A word that can be part of the claim's own subject: a bridge word or a known
    provider name."""
    return word.lower() in _BRIDGE or word.lower() in names.words


def _condition_exempts(sentence: str, match: re.Match[str], names: _Names) -> bool:
    """True when a condition in the claim's own clause exempts it (10.8).

    The condition clause may end in one comma ("Once you approve, it will be booked");
    any other break (but, and, so, a second comma, a semicolon) ends its reach. Inside
    that reach a claim is exempt when its clause is future or modal, or when the
    condition is the user's present-tense approval and the claim is present. An "if" or
    "unless" whose subject leads straight to the claim verb ("if it's booked") is a
    hypothetical; "unless you object I have reserved it" is not. A present claim under the
    approval may take a subject pronoun and a be-form ("Once you approve, you're all set").
    """
    before = sentence[: match.start()]
    cond = list(_CONDITIONAL.finditer(before))
    if not cond:
        return False
    span = before[cond[-1].start() :]
    _, comma, tail = span.partition(",")
    claim_clause = tail if comma else span
    if _CLAUSE_BREAK.search(claim_clause):
        return False
    if not comma and _HYPOTHETICAL.match(span):
        between = re.findall(r"[\w']+", span.lower())[1:]
        if all(w in _BRIDGE for w in between):
            return True
    if _modal_reaches_claim(claim_clause, names) or re.match(
        r"(?:will|'ll)\b", match.group(0), re.I
    ):
        return True
    approval = _APPROVAL_CONDITION.match(span)
    if not approval or not _present_claim(claim_clause, match.group(0)):
        return False
    # Only the claim's subject may sit between the approval and the claim verb ("once you
    # approve Sharma is booked"), never a verb of its own ("I can see it is confirmed").
    gap = claim_clause if comma else span[approval.end() :]
    gap = _SUBJECT_BE.sub(" ", names.mask(gap))
    return all(
        w.lower() in _APPROVAL_OBJECT or _is_subject_word(w, names)
        for w in re.findall(r"[\w']+", gap)
    )


def _scoped_negation(sentence: str, match: re.Match[str], names: _Names) -> bool:
    """True when a negation, condition or future frame scopes over this claim match."""
    before = sentence[: match.start()]
    if _negated_before(_CLAUSE_BREAK.split(before)[-1]):
        return True
    if _TRAILING_NO.match(sentence[match.end() :]):
        return True
    if _FUTURE.search(before):
        return True
    return _condition_exempts(sentence, match, names)


def _real_claim(sentence: str, match: re.Match[str], names: _Names) -> bool:
    word = match.group(0).lower()
    rest = sentence[match.end() :]
    if word == "done" and _DONE_OBJECT.match(rest):
        return False
    if word == "sorted" and _SORTED_BY.match(rest):
        return False
    return not _scoped_negation(sentence, match, names)


def _verb(sentence: str, names: _Names) -> Kind | None:
    """The kind of record the sentence claims, if any claim in it is not negated."""
    for kind, rx in (("cancel", _CANCEL), ("reschedule", _RESCHEDULE), ("book", _BOOK)):
        if any(_real_claim(sentence, m, names) for m in rx.finditer(sentence)):
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
    on the current shortlist to its name, so naming an unbooked one is caught too; the
    providers of `records` (the user's known bookings) are added. Those names, and no
    other, may bridge a modal to a claim verb."""
    names = dict(provider_names or {})
    for r in records:
        names.setdefault(r.provider_id, r.provider_name)
    bridgeable = _Names.of(names)
    kept: list[str] = []
    removed: list[str] = []
    for sentence in _SENTENCE.split(text.strip()):
        if not sentence.strip():
            continue
        kind = _verb(sentence, bridgeable)
        if kind is None or _backed(sentence, kind, records, names, tz):
            kept.append(sentence.strip())
        else:
            removed.append(sentence.strip())
    return Guarded(" ".join(kept), removed)
