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
    """Drop what a stale memory row put in the brief; returns the field names touched.

    A fact whose `prov.memory_id` is stale goes to None. A window whose time bound came
    from a stale row goes to None too here, because this form cannot re-resolve it (the
    run-time path, `apply_memory`, drops only the bound). A provider preference loses only
    the component whose row is stale.
    """
    cleared: list[str] = []
    for name in FACT_FIELDS:
        fact = data.get(name)
        if not isinstance(fact, dict):
            continue
        prov = fact.get("prov")
        memory_id = prov.get("memory_id") if isinstance(prov, dict) else None
        own = memory_id is not None and stale(memory_id)
        bound = name == "window" and any(stale(m) for m in _bound_ids(fact))
        if own or bound:
            data[name] = None
            cleared.append(name)
        elif name == "provider_pref" and _clear_pref(fact, stale):
            if not _pref_left(fact):
                data[name] = None
            cleared.append(name)
    return cleared


def _bound_ids(window: dict[str, Any]) -> list[str]:
    ids = window.get("bound_memory_ids")
    return [str(v) for v in ids.values()] if isinstance(ids, dict) else []


def _clear_pref(fact: dict[str, Any], stale: Callable[[str], bool]) -> bool:
    """Drop a stale component of a stored provider preference; True if one was dropped."""
    value = fact.get("value")
    if not isinstance(value, dict):
        return False
    changed = False
    prefer, avoid = fact.get("prefer_memory_id"), fact.get("avoid_memory_id")
    if prefer is not None and stale(prefer):
        value["prefer_id"] = None
        fact["prefer_memory_id"] = None
        changed = True
    if avoid is not None and stale(avoid):
        value["avoid_ids"] = []
        fact["avoid_memory_id"] = None
        changed = True
    return changed


def _pref_left(fact: dict[str, Any]) -> bool:
    value = fact.get("value")
    return isinstance(value, dict) and bool(value.get("prefer_id") or value.get("avoid_ids"))


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
