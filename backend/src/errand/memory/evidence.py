"""Evidence for an explicit write: the quote, where it sits, and what it says.

`check_explicit` is the one gate `service.remember_explicit` runs. It needs, in order:
- a quote of at least 4 characters that is a substring of the user's message, compared
  with case, whitespace and Unicode normalised, and located so its offsets in the
  original text can be stored (redaction needs them);
- a standing-preference cue in the quote and no one-off veto anywhere in the sentence
  that holds the quote (cues.py; the planner's choice of span is not trusted);
- the value in the quote.

The value check is built on the pure parsers of `errand.domain.evidence` (the same ones
`update_brief` uses, architecture 10.7): a time parses out of the span to the same time,
an amount to the same rupees, an area matches its alias, a provider matches by id or by a
name in the catalogue view the agent holds, weekdays by name. A surface-form match is the
fallback for keys with no parser (language, access note, contact window).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from typing import Any

from errand.domain import aliases
from errand.domain import evidence as parsers
from errand.domain.aliases import AREA_ALIASES
from errand.memory.cues import check_span, normalise, sentence_around
from errand.memory.registry import WEEKDAYS, surface_forms

MIN_QUOTE_CHARS = 4

ValueCheck = Callable[[str, str, Any, str], bool]
"""(key, scope, value, quote) -> does the quote carry the value"""


class EvidenceRefused(ValueError):
    """Why a write has no evidence; the text quotes the user's words back to the model."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Located:
    start: int
    end: int
    quote: str


def locate_quote(message: str, quote: str) -> Located | None:
    """Find `quote` in `message`, ignoring case and whitespace differences.

    Returns offsets into the original `message`, and the text found there (which is
    what gets stored, not what the model typed).
    """
    words = unicodedata.normalize("NFKC", quote).split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(w) for w in words)
    for haystack in (message, unicodedata.normalize("NFKC", message)):
        if len(haystack) != len(message):
            continue  # offsets would not map back to the original text
        found = re.search(pattern, haystack, flags=re.IGNORECASE)
        if found is not None:
            return Located(found.start(), found.end(), message[found.start() : found.end()])
    return None


def _surface(key: str, value: Any, quote: str) -> bool:
    text = normalise(quote)
    return any(
        re.search(rf"(?<![\w]){re.escape(normalise(form))}(?![\w])", text) is not None
        for form in surface_forms(key, value)
    )


def _hhmm(value: Any) -> time | None:
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def value_in_quote(key: str, scope: str, value: Any, quote: str) -> bool:
    """Does the quote carry the value? Parsers first, surface forms as the fallback."""
    text = normalise(quote)
    if key in ("earliest_time", "latest_time"):
        want = _hhmm(value)
        return want is not None and want in parsers.parse_times(text)
    if key == "budget_ceiling":
        return int(value) in parsers.parse_amounts(text)
    if key == "home_area":
        names = aliases.aliases_of("area", str(value))
        return any(name in text for name in names) or _surface(key, value, quote)
    if key == "avoid_days":
        days = {d for d in WEEKDAYS if re.search(rf"\b{d}s?\b|\b{d[:3]}\b", text)}
        return bool(value) and set(value) <= days
    return _surface(key, value, quote)


@dataclass(frozen=True)
class Checked:
    start: int
    end: int
    quote: str
    cue: str


def check_explicit(
    *,
    message: str,
    quote: str,
    key: str,
    scope: str,
    value: Any,
    value_check: ValueCheck = value_in_quote,
) -> Checked:
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        raise EvidenceRefused("quote_too_short", "the quote must be at least 4 characters")
    located = locate_quote(message, quote)
    if located is None:
        raise EvidenceRefused(
            "quote_not_in_message",
            f"{quote!r} is not in the user's message; quote their own words",
        )
    # the cue must be in the quote; the one-off veto reads the whole sentence around it
    sentence = sentence_around(message, located.start, located.end)
    cue = check_span(located.quote, key, veto_text=sentence)
    if cue.veto is not None:
        raise EvidenceRefused(
            "one_off",
            f"{sentence.strip()!r} is a one-off ({cue.veto!r}), not a standing preference",
        )
    if cue.cue is None:
        raise EvidenceRefused(
            "no_cue", f"{located.quote!r} does not say it is a standing preference"
        )
    if not value_check(key, scope, value, located.quote):
        raise EvidenceRefused("value_not_in_quote", f"{located.quote!r} does not state that value")
    return Checked(located.start, located.end, located.quote, cue.cue)


def value_from_span(key: str, text: str) -> Any | None:
    """The one value a span states for `key`, or None (a click re-reads the user's words).

    Used by the "Remember this?" and "Update your saved ...?" chips: they hold only the span,
    so a forgotten value cannot survive in a chip. None when the span states no value, or an
    ambiguous one (a bare "5" is 05:00 or 17:00), or was redacted since.
    """
    norm = normalise(text)
    if key in ("earliest_time", "latest_time"):
        found = parsers.parse_times(norm)
        return (
            f"{next(iter(found)).hour:02d}:{next(iter(found)).minute:02d}"
            if len(found) == 1
            else None
        )
    if key == "budget_ceiling":
        amounts = parsers.parse_amounts(norm)
        return next(iter(amounts)) if len(amounts) == 1 else None
    if key == "home_area":
        hits = [a for a in AREA_ALIASES if any(n in norm for n in aliases.aliases_of("area", a))]
        return hits[0] if len(hits) == 1 else None
    if key == "avoid_days":
        days = [d for d in WEEKDAYS if re.search(rf"\b{d}s?\b|\b{d[:3]}\b", norm)]
        return days or None
    return None
