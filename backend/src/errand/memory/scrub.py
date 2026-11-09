"""Drop brief fields whose memory is no longer eligible (gate 2 decision 10).

Pure over a Brief and the set of eligible memory ids. A field with `prov.memory_id` set
that is not in the set is missing as far as anyone can tell: the builder never renders
it, ingress clears it, and every write of a brief into agent.sqlite goes through a scrub
so a stale checkpoint can never put a forgotten value back. The stored-JSON form of the
same rule is `briefjson.scrub_brief_json`.
"""

from __future__ import annotations

from collections.abc import Collection

from errand.domain.brief import Brief
from errand.memory.briefjson import FACT_FIELDS, clear_fields, scrub_brief_json

__all__ = ["clear_fields", "scrub_brief", "scrub_brief_json", "stale_fields"]


def stale_fields(brief: Brief, eligible: Collection[str]) -> list[str]:
    """Names of the brief fields sourced from a memory that is not eligible."""
    out: list[str] = []
    for name in FACT_FIELDS:
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
