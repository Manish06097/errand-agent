"""Cleaning world text before the planner sees it (architecture 11.1).

Applied when a tool result is turned into its planner view, which happens before the
result is appended to the loop, never after: the loop history is append-only.
- control, zero-width and bidi characters are removed (a review once hid an
  instruction behind zero-width joiners);
- whitespace is collapsed and every string is capped (names 60, attributes 120);
- names keep a conservative charset;
- phone numbers, addresses and keys are masked with the tracer's patterns.
"""

from __future__ import annotations

import re
import unicodedata

from errand.trace.sanitize import sanitize_text

NAME_CAP = 60
ATTRIBUTE_CAP = 120
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_NAME_CHARS = re.compile(r"[^\w .,&'()/-]", flags=re.UNICODE)


def _strip_controls(text: str) -> str:
    text = _INVISIBLE.sub("", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")


def cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def clean_text(text: str, limit: int = ATTRIBUTE_CAP) -> str:
    text = unicodedata.normalize("NFKC", _strip_controls(text))
    text = re.sub(r"\s+", " ", text).strip()
    return cap(sanitize_text(text), limit)


def clean_name(text: str) -> str:
    text = clean_text(text, limit=10 * NAME_CAP)
    return cap(_NAME_CHARS.sub("", text).strip(), NAME_CAP)
