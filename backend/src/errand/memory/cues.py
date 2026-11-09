"""The standing-preference cue table (gate 2 decision 3, architecture 10.7).

Pure and unit-tested. A span of the user's message is a standing preference only if it
carries a cue ("always", "never", "from now on", "every time", "I live in", "I prefer",
"my usual", or a key-specific cue such as "I moved to" for `home_area`) and no one-off
marker in the same span ("this time", "today", "for now", "just for") vetoes it.
"this time I prefer after 5" therefore saves nothing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

GENERAL_CUES: tuple[str, ...] = (
    "always",
    "never",
    "from now on",
    "every time",
    "i live in",
    "i prefer",
    "my usual",
)
KEY_CUES: dict[str, tuple[str, ...]] = {
    "home_area": ("i moved to", "i've moved to", "i have moved to", "we moved to"),
}
ONE_OFF_MARKERS: tuple[str, ...] = ("this time", "today", "for now", "just for")


_QUOTES = str.maketrans({0x2019: "'", 0x2018: "'"})


def normalise(text: str) -> str:
    """NFKC, case-folded, curly quotes straightened, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip()


def _find(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(phrase)}(?![\w])", text) is not None


@dataclass(frozen=True)
class CueResult:
    cue: str | None
    veto: str | None

    @property
    def ok(self) -> bool:
        return self.cue is not None and self.veto is None


def check_span(span: str, key: str) -> CueResult:
    """The first cue in the span (general cues, then the key's own) and the first veto."""
    text = normalise(span)
    cue = next((c for c in (*GENERAL_CUES, *KEY_CUES.get(key, ())) if _find(c, text)), None)
    veto = next((v for v in ONE_OFF_MARKERS if _find(v, text)), None)
    return CueResult(cue, veto)
