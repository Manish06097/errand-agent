"""Spans to redact, and the text edits that make them final (gate 2 decisions 9 and 13).

Pure helpers; `service.forget` and `service.correct` do the SQL. A forget overwrites the
spans in `messages.body_json`, so after it agent.sqlite holds no plaintext copy of the
value. A correct only hides the old evidence span from the model view (kind
`superseded`): the transcript still shows what the user wrote.

Offsets in `message_redactions` are into the message's primary text (`primary_text`).
After an overwrite they describe the marker in the new text, and `shift_after` moves the
rows that follow an edit, so a second forget on the same message stays consistent.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from typing import Any

FORGOTTEN_MARKER = "[removed at your request]"
SUPERSEDED_MARKER = "[updated preference]"

# User-written and model-written strings in a message body, by body type (decision 13).
# The first is the primary text that redaction offsets refer to; a clarify card's options
# are model-written too and are replaced without a row.
#
# Notices are in scope: the step-limit notice restates the brief ("So far I have: plumber,
# Indiranagar"), which can hold a memory-pre-filled value, and every other notice is fixed
# wording so over-masking costs nothing (ruling in 12.4, review round 3).
#
# Other text that code renders is out of scope on purpose: an outcome or reminder line,
# a status line, a card title. They record what happened ("Booked Ravi, Thu 10:00"), which
# is a fact about a booking and not a copy of the preference. Forgetting "not before 10"
# must not wipe "10:00" from them. The trade-off, accepted by the ruling in 12.4: a value
# that the code later rendered into such a line (a home area in a status line) stays in
# agent.sqlite until the errand is deleted; the model never sees it as a preference, since
# the brief scrub and the builder's eligible view see to that.
_PRIMARY: dict[str, str] = {
    "text": "text",
    "notice": "text",
    "clarify": "question",
    "shortlist": "recommendation",
}


def primary_key(body: dict[str, Any]) -> str | None:
    return _PRIMARY.get(str(body.get("type", "text")))


def primary_text(body: dict[str, Any]) -> str | None:
    key = primary_key(body)
    value = body.get(key) if key else None
    return value if isinstance(value, str) else None


def find_spans(text: str, forms: Iterable[str]) -> list[tuple[int, int]]:
    """Non-overlapping spans of `text` holding any of `forms`, word-bounded, any case.

    Longer forms win over shorter ones that start at the same place.
    """
    ordered = sorted({f for f in forms if f.strip()}, key=len, reverse=True)
    if not ordered:
        return []
    pattern = "|".join(re.escape(f) for f in ordered)
    return [
        (m.start(), m.end())
        for m in re.finditer(rf"(?<!\w)(?:{pattern})(?!\w)", text, flags=re.IGNORECASE)
    ]


def merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def overwrite(text: str, spans: Sequence[tuple[int, int]], marker: str) -> str:
    """Replace each (merged) span by `marker`."""
    pieces: list[str] = []
    cursor = 0
    for start, end in merge(spans):
        pieces.append(text[cursor:start])
        pieces.append(marker)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def new_spans(spans: Sequence[tuple[int, int]], marker: str) -> list[tuple[int, int]]:
    """Where the markers sit in the text after `overwrite(text, spans, marker)`."""
    out: list[tuple[int, int]] = []
    shift = 0
    for start, end in merge(spans):
        out.append((start + shift, start + shift + len(marker)))
        shift += len(marker) - (end - start)
    return out


def shift_after(
    row: tuple[int, int], edits: Sequence[tuple[int, int]], marker_len: int
) -> tuple[int, int] | None:
    """Move an existing redaction row across edits made to the same text.

    `edits` are the spans that were overwritten, in the old text's coordinates. A row
    that overlaps an edit is dropped (None): the edit swallowed it.
    """
    start, end = row
    shift = 0
    for e_start, e_end in merge(edits):
        if e_end <= start:
            shift += marker_len - (e_end - e_start)
        elif e_start < end:
            return None
    return start + shift, end + shift


def apply_model_view(text: str, rows: Iterable[tuple[int, int, str]]) -> str:
    """The text as the model sees it: superseded spans become '[updated preference]'.

    Forgotten spans are overwritten in storage already, so a row of that kind only
    keeps its own marker text.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end, kind in sorted(rows):
        if start < cursor or end > len(text):
            continue
        pieces.append(text[cursor:start])
        pieces.append(SUPERSEDED_MARKER if kind == "superseded" else text[start:end])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redact_body(
    body: dict[str, Any], forms: Iterable[str], extra_primary: Sequence[tuple[int, int]] = ()
) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    """Overwrite every surface form (and `extra_primary` offsets) in a message body.

    Returns the new body and the primary-text spans that were edited, in the old text's
    coordinates. A clarify card's options are replaced without a row: the redaction table
    has no coordinates for them. Brief chips on a card are redacted in `value` and `evidence`;
    other code-rendered fields are never touched.
    """
    forms = tuple(forms)
    new = dict(body)
    key = primary_key(body)
    edited: list[tuple[int, int]] = []
    if key is not None and isinstance(body.get(key), str):
        text: str = body[key]
        edited = merge([*find_spans(text, forms), *extra_primary])
        if edited:
            new[key] = overwrite(text, edited, FORGOTTEN_MARKER)
    options = body.get("options") if body.get("type") == "clarify" else None
    if isinstance(options, list):
        new["options"] = [
            overwrite(o, find_spans(o, forms), FORGOTTEN_MARKER) if isinstance(o, str) else o
            for o in options
        ]
    chips = body.get("brief")
    if isinstance(chips, list):
        # M8 brief chips on a card restate the brief: a memory-sourced value and the
        # user's cut evidence. The window_label is the resolved search window, left alone.
        new["brief"] = [_redact_chip(c, forms) for c in chips]
    return new, edited


def redact_card_chips(
    card: dict[str, Any],
    forms: Sequence[str],
    *,
    blank_fields: Collection[str] = (),
    marker: str = FORGOTTEN_MARKER,
) -> tuple[dict[str, Any], bool]:
    """An approval card with its brief chips redacted: every surface form in `value` and
    `evidence`, and the whole value of a memory-sourced chip on one of `blank_fields`.
    Nothing else on the card is touched (the fingerprinted material is not here)."""
    chips = card.get("brief")
    if not isinstance(chips, list):
        return card, False
    out: list[Any] = []
    for chip in chips:
        new = _redact_chip(chip, forms)
        if (
            isinstance(new, dict)
            and new.get("source") == "memory"
            and new.get("field") in blank_fields
        ):
            new = {**new, "value": marker, "evidence": None}
        out.append(new)
    return ({**card, "brief": out}, True) if out != chips else (card, False)


def _redact_chip(chip: Any, forms: Sequence[str]) -> Any:
    if not isinstance(chip, dict):
        return chip
    out = dict(chip)
    for name in ("value", "evidence"):
        text = chip.get(name)
        if isinstance(text, str):
            out[name] = overwrite(text, find_spans(text, forms), FORGOTTEN_MARKER)
    return out
