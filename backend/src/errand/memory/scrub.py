"""Drop brief fields whose memory is no longer eligible (gate 2 decision 10).

Pure over a Brief and the set of eligible memory ids. A field with `prov.memory_id` set
that is not in the set is missing as far as anyone can tell: the builder never renders
it, ingress clears it, and every write of a brief into agent.sqlite goes through here so
a stale checkpoint can never put a forgotten value back.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from errand.domain.brief import Brief

_FACT_FIELDS = (
    "service",
    "issue",
    "area",
    "window",
    "budget_max_inr",
    "provider_pref",
    "target_booking_id",
    "new_window",
)


def stale_fields(brief: Brief, eligible: Collection[str]) -> list[str]:
    """Names of the brief fields sourced from a memory that is not eligible."""
    out: list[str] = []
    for name in _FACT_FIELDS:
        fact = getattr(brief, name)
        memory_id = fact.prov.memory_id if fact is not None else None
        if memory_id is not None and memory_id not in eligible:
            out.append(name)
    return out


def scrub_brief(brief: Brief, eligible: Collection[str]) -> tuple[Brief, list[str]]:
    """The brief without its stale fields, and which fields were cleared."""
    cleared = stale_fields(brief, eligible)
    if not cleared:
        return brief, []
    return brief.model_copy(update={name: None for name in cleared}), cleared


def scrub_brief_json(raw: str, eligible: Collection[str]) -> tuple[str, list[str]]:
    """The same over a stored brief (`origin_brief_json`, `errands.brief_json`).

    Works on the JSON itself so it needs no model validation of an old row.
    """
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        return raw, []
    cleared = clear_fields(data, lambda memory_id: memory_id not in eligible)
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) if cleared else raw
    ), cleared


def clear_fields(data: dict[str, Any], stale: Any) -> list[str]:
    cleared: list[str] = []
    for name in _FACT_FIELDS:
        fact = data.get(name)
        if not isinstance(fact, dict):
            continue
        prov = fact.get("prov")
        memory_id = prov.get("memory_id") if isinstance(prov, dict) else None
        if memory_id is not None and stale(memory_id):
            data[name] = None
            cleared.append(name)
    return cleared
