"""Cleaning world text before a model sees it (architecture 11.1).

Applied when a tool result is turned into its planner view, which happens before the
result is appended to the loop, never after: the loop history is append-only. Review
text goes through `clean_review` before the quarantined reader sees it.
- control, zero-width, bidi and other invisible characters are removed (a review once
  hid an instruction behind zero-width joiners);
- whitespace is collapsed and every string is capped (names 60, attributes 120,
  reviews 1,000);
- names keep a conservative charset;
- phone numbers, addresses and keys are masked with the tracer's patterns.
"""

from __future__ import annotations

import re
import unicodedata

from errand.trace.sanitize import sanitize_text

NAME_CAP = 60
ATTRIBUTE_CAP = 120
REVIEW_CAP = 1_000

# Characters that render as nothing (or next to nothing) and so can hide text from a
# human reader while a model still reads it:
# - U+00AD soft hyphen, U+034F combining grapheme joiner, U+180E Mongolian vowel
#   separator;
# - U+200B..U+200F zero-width space, ZWNJ, ZWJ and the LTR/RTL marks;
# - U+202A..U+202E and U+2066..U+2069 bidi embeddings, overrides and isolates;
# - U+2060..U+2064 word joiner and the invisible operators;
# - U+FE00..U+FE0F and U+E0100..U+E01EF variation selectors;
# - U+FEFF byte order mark / zero-width no-break space;
# - U+E0000..U+E007F tag characters, which can spell out a whole ASCII instruction
#   invisibly.
# Stripping ZWJ and ZWNJ changes how some Indic scripts render (Devanagari half forms,
# Malayalam chillu letters): the text stays readable, but a few conjuncts display
# differently. We accept that for untrusted provider text, because these two are
# exactly the characters used to hide instructions, and nothing downstream needs the
# exact glyph shape of a review or a provider name.
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069"
    "\ufe00-\ufe0f\ufeff\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
_NAME_CHARS = re.compile(r"[^\w .,&'()/-]", flags=re.UNICODE)


def strip_invisible(text: str) -> str:
    """Remove invisible and control characters; keep newlines and tabs."""
    text = _INVISIBLE.sub("", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")


def cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def clean_text(text: str, limit: int = ATTRIBUTE_CAP) -> str:
    text = unicodedata.normalize("NFKC", strip_invisible(text))
    # Strip again after NFKC: nothing promises normalisation stays out of the set, and
    # the second pass is cheap.
    text = strip_invisible(text)
    text = re.sub(r"\s+", " ", text).strip()
    return cap(sanitize_text(text), limit)


def clean_review(text: str) -> str:
    """A review as the quarantined reader sees it: cleaned and capped at 1,000 chars."""
    return clean_text(text, limit=REVIEW_CAP)


def clean_name(text: str) -> str:
    text = clean_text(text, limit=10 * NAME_CAP)
    return cap(_NAME_CHARS.sub("", text).strip(), NAME_CAP)
