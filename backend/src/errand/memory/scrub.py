"""Drop brief fields whose memory is no longer eligible (gate 2 decision 10).

Pure over a Brief and the set of eligible memory ids. A field with `prov.memory_id` set
that is not in the set is missing as far as anyone can tell: the builder never renders
it, ingress clears it, and every write of a brief into agent.sqlite goes through a scrub
so a stale checkpoint can never put a forgotten value back. The stored-JSON form of the
same rule is `briefjson.scrub_brief_json`.
"""

from __future__ import annotations

from collections.abc import Collection

from errand.domain.brief import Brief, ProviderPref, ProviderPrefFact, WindowFact
from errand.memory.briefjson import FACT_FIELDS, clear_fields, scrub_brief_json

__all__ = ["clear_fields", "scrub_brief", "scrub_brief_json", "stale_fields"]


def _stale_pref(fact: ProviderPrefFact, eligible: Collection[str]) -> tuple[bool, bool]:
    """(prefer component stale, avoid component stale)."""
    prefer, avoid = fact.prefer_memory_id, fact.avoid_memory_id
    return (
        prefer is not None and prefer not in eligible,
        avoid is not None and avoid not in eligible,
    )


def stale_fields(brief: Brief, eligible: Collection[str]) -> list[str]:
    """Names of the brief fields sourced from a memory that is not eligible."""
    out: list[str] = []
    for name in FACT_FIELDS:
        fact = getattr(brief, name)
        if fact is None:
            continue
        memory_id = fact.prov.memory_id
        own = memory_id is not None and memory_id not in eligible
        bound = isinstance(fact, WindowFact) and any(
            m not in eligible for m in fact.bound_memory_ids.values()
        )
        pref = isinstance(fact, ProviderPrefFact) and any(_stale_pref(fact, eligible))
        if own or bound or pref:
            out.append(name)
    return out


def scrub_brief(brief: Brief, eligible: Collection[str]) -> tuple[Brief, list[str]]:
    """The brief without its stale fields, and which fields were cleared.

    A window with a stale time bound is cleared whole here (this pure form cannot
    re-resolve it); `apply_memory` drops only the bound before this runs. A provider
    preference loses only the component whose row is stale.
    """
    cleared = stale_fields(brief, eligible)
    if not cleared:
        return brief, []
    update: dict[str, object] = {}
    for name in cleared:
        fact = getattr(brief, name)
        if isinstance(fact, ProviderPrefFact) and fact.prov.memory_id is None:
            drop_prefer, drop_avoid = _stale_pref(fact, eligible)
            value = ProviderPref(
                prefer_id=None if drop_prefer else fact.value.prefer_id,
                avoid_ids=[] if drop_avoid else list(fact.value.avoid_ids),
            )
            update[name] = (
                fact.model_copy(
                    update={
                        "value": value,
                        "prefer_memory_id": None if drop_prefer else fact.prefer_memory_id,
                        "avoid_memory_id": None if drop_avoid else fact.avoid_memory_id,
                    }
                )
                if value.prefer_id or value.avoid_ids
                else None
            )
        else:
            update[name] = None
    return brief.model_copy(update=update), cleared
