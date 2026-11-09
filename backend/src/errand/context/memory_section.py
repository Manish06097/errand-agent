"""S4: the chosen memories as a JSON data block (13.1, gate 2 decision 6).

Rendered in registry order, so the bytes change only when the set does. The block is
data, like `<tool_data>`: S1 says so, and `access_note`, the one free-text value, can say
anything. Values are re-validated by the registry when selected and rendered from their
canonical form, never from the stored text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from errand.context.selection import Chosen, Selection, select
from errand.llm.tokens import est_tokens
from errand.store.repos.memories import MemoryRow

S4_CAP = 600


def render_section(chosen: Sequence[Chosen]) -> str:
    """ "" when there is nothing to say; otherwise the block."""
    if not chosen:
        return ""
    items = [_item(c) for c in chosen]
    body = _escape(json.dumps(items, ensure_ascii=False, separators=(",", ":")))
    return f"<memory>\n{body}\n</memory>"


_HTML = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}


def _escape(body: str) -> str:
    """JSON-escape the characters that could end the data block or open a tag.

    json.dumps leaves < > & alone. These are valid JSON escapes, so the block still parses
    to the same value, but no stored text can write `</memory>` into the request. The
    registry also refuses angle brackets in a note; this is the second wall.
    """
    return "".join(_HTML.get(ch, ch) for ch in body)


def _item(c: Chosen) -> dict[str, object]:
    """{"home_area": "indiranagar", "id": "m-01"}, or with "service" for a service-scoped row.

    The key names are the registry's, none of which is "service", so the tag is never
    ambiguous. This is the most compact form that still says which scope a row has.
    """
    item: dict[str, object] = {c.row.key: c.value}
    if c.row.scope != "global":
        item["service"] = c.row.scope.split(":", 1)[1]
    # The handle `propose_forget` names a row by: an id, never a value (N2B).
    item["id"] = c.row.memory_id
    return item


def measure(chosen: Sequence[Chosen]) -> int:
    text = render_section(chosen)
    return est_tokens(text) if text else 0


def build_memory_section(
    rows: Sequence[MemoryRow],
    *,
    errand_kind: str,
    service: str | None,
    now_ms: int,
    half_life_days: float = 60.0,
    cap_tokens: int = S4_CAP,
) -> tuple[str, Selection]:
    selection = select(
        rows,
        errand_kind=errand_kind,
        service=service,
        now_ms=now_ms,
        half_life_days=half_life_days,
        cap_tokens=cap_tokens,
        measure=measure,
    )
    return render_section(selection.included), selection
