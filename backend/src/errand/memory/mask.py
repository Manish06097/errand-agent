"""The n-gram mask (gate 2 decisions 13 and 14, architecture 12.3 step 5).

Pure. It finds runs of words whose normalised HMAC equals a hash the forget wrote
(`memory_surface_hashes`) and replaces them, so a forgotten value that survives in text
written before the forget (a user message, a model-written card sentence, a past trace
record) never leaves the process. The caller decides what text is in scope; code-rendered
material is never passed through.

Normalisation is shared by the writer of the hashes and the reader: NFKC, case-folded
word tokens, punctuation dropped, joined by one space. Only this module and
`redaction.py` turn text into hash inputs, so both sides always agree.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from errand.memory.hmac_key import hmac_hex

MAX_NGRAM = 24
MARKER = "[removed]"
_TOKEN = re.compile(r"[^\W_]+(?:[:.,'][^\W_]+)*")


def _fold(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(folded token, start, end) for each word of `text`, offsets into `text`."""
    return [(_fold(m.group()), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def normal_form(text: str) -> str:
    return " ".join(tok for tok, _, _ in tokens(text))


def hash_of(key: bytes, text: str) -> str | None:
    """The HMAC of `text` as the mask would see it; None when it has no words."""
    form = normal_form(text)
    return hmac_hex(key, form) if form else None


@dataclass(frozen=True)
class MaskResult:
    text: str
    masked: int = 0
    memory_ids: tuple[str, ...] = field(default_factory=tuple)


def mask_text(
    text: str,
    key: bytes,
    hashes: Mapping[str, str],
    *,
    marker: str = MARKER,
    max_ngram: int = MAX_NGRAM,
) -> MaskResult:
    """Replace every word n-gram whose HMAC is in `hashes` (hmac -> memory_id).

    At each position the longest match wins; matches never overlap. Over-masking is
    accepted by the design: an old "Indiranagar" about something else is masked too.
    """
    if not hashes:
        return MaskResult(text)
    toks = tokens(text)
    out: list[str] = []
    ids: list[str] = []
    cursor = 0
    i = 0
    count = 0
    while i < len(toks):
        hit = 0
        owner = ""
        top = min(max_ngram, len(toks) - i)
        for n in range(top, 0, -1):
            digest = hmac_hex(key, " ".join(t for t, _, _ in toks[i : i + n]))
            if digest in hashes:
                hit, owner = n, hashes[digest]
                break
        if hit == 0:
            i += 1
            continue
        start, end = toks[i][1], toks[i + hit - 1][2]
        out.append(text[cursor:start])
        out.append(marker)
        cursor = end
        ids.append(owner)
        count += 1
        i += hit
    out.append(text[cursor:])
    return MaskResult("".join(out), count, tuple(dict.fromkeys(ids)))
