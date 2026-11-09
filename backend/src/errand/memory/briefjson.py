"""The stored-JSON half of the brief scrub, free of every errand.domain import.

`errand.store` needs it (a repo scrubs `origin_brief_json` on write), and the store layer
may not import the domain models. `scrub.py` is the same rule over a `Brief` object.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from typing import Any

FACT_FIELDS = (
    "service",
    "issue",
    "area",
    "window",
    "budget_max_inr",
    "provider_pref",
    "target_booking_id",
    "new_window",
)


def clear_fields(data: dict[str, Any], stale: Callable[[str], bool]) -> list[str]:
    """Set to None every fact whose `prov.memory_id` is stale; returns the field names."""
    cleared: list[str] = []
    for name in FACT_FIELDS:
        fact = data.get(name)
        if not isinstance(fact, dict):
            continue
        prov = fact.get("prov")
        memory_id = prov.get("memory_id") if isinstance(prov, dict) else None
        if memory_id is not None and stale(memory_id):
            data[name] = None
            cleared.append(name)
    return cleared


def scrub_brief_json(raw: str, eligible: Collection[str]) -> tuple[str, list[str]]:
    """A stored brief without the fields whose memory is not eligible.

    Works on the JSON itself so it needs no model validation of an old row.
    """
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        return raw, []
    cleared = clear_fields(data, lambda memory_id: memory_id not in eligible)
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) if cleared else raw
    ), cleared
