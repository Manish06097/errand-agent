"""Evidence for an explicit write: the quote, where it sits, and what it says.

`check_explicit` is the one gate `service.remember_explicit` runs. It needs, in order:
- a quote of at least 4 characters that is a substring of the user's message, compared
  with case, whitespace and Unicode normalised, and located so its offsets in the
  original text can be stored (redaction needs them);
- a standing-preference cue in the quote and no one-off veto (cues.py);
- the value in the quote.

The value check here is the conservative one: a surface form of the value must appear
in the quote. Architecture 10.7 wants the evidence parsers of `agent/evidence.py` (a
time parsed out of "not before ten", an amount out of "under 1,500"); those land with
M8 and replace `value_in_quote` through the `value_check` argument, so a paraphrase
this check refuses simply becomes a "Remember this for next time?" chip until then.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from errand.memory.cues import check_span, normalise
from errand.memory.registry import surface_forms

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


def value_in_quote(key: str, scope: str, value: Any, quote: str) -> bool:
    text = normalise(quote)
    return any(
        re.search(rf"(?<![\w]){re.escape(normalise(form))}(?![\w])", text) is not None
        for form in surface_forms(key, value)
    )


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
    cue = check_span(located.quote, key)
    if cue.veto is not None:
        raise EvidenceRefused(
            "one_off", f"{located.quote!r} is a one-off ({cue.veto!r}), not a standing preference"
        )
    if cue.cue is None:
        raise EvidenceRefused(
            "no_cue", f"{located.quote!r} does not say it is a standing preference"
        )
    if not value_check(key, scope, value, located.quote):
        raise EvidenceRefused("value_not_in_quote", f"{located.quote!r} does not state that value")
    return Checked(located.start, located.end, located.quote, cue.cue)
