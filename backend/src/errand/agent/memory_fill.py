"""Memory in the brief at the start of a run (gate 2 decisions 10, 11 and 17).

`apply_memory` runs in `ingress`, before anything reads the brief:

1. **Clear.** A brief field whose `prov.memory_id` is no longer eligible is cleared, an
   `invalidate` record is written for it, and if the row was corrected, the field is
   re-filled from its active successor. This is how a forget or a correct that landed
   after the checkpoint was written reaches the open errand (the checkpoint is a
   different file from the forget's transaction). On that turn `missing()` contains the
   field and the agent asks for it (S49).
2. **Pre-fill.** At the start of an errand, a required field with a matching global row
   is filled by code with `prov.source = "memory"` and `prov.memory_id`, shown as
   "(from memory)". A field the user later clears is not refilled in that errand, and
   the current message beats memory because `update_brief` merges over it.

Service-scoped keys (a plumbing budget ceiling) are filled inside `update_brief`'s merge
when the service is set, because the service is not known yet at ingress (phase B).
"""

from __future__ import annotations

from typing import Any

from errand.agent.deps import RunDeps
from errand.domain.brief import AreaFact, Brief, ErrandKind, Provenance
from errand.domain.errand import Errand
from errand.memory.registry import GLOBAL, RegistryError, spec_for
from errand.memory.scrub import scrub_brief, stale_fields
from errand.store.repos.memories import MemoryRow
from errand.world.contracts import Area


def _active_successor(rows: dict[str, MemoryRow], memory_id: str) -> MemoryRow | None:
    row = rows.get(memory_id)
    seen: set[str] = set()
    while row is not None and row.status == "superseded" and row.superseded_by:
        if row.memory_id in seen:
            return None
        seen.add(row.memory_id)
        row = rows.get(row.superseded_by)
    return row if row is not None and row.status == "active" else None


def _area_from(row: MemoryRow, turn: int) -> AreaFact | None:
    try:
        spec = spec_for(row.key)
        area = Area(spec.parse(row.value))
    except (RegistryError, ValueError, TypeError):
        return None
    prov = Provenance(source="memory", memory_id=row.memory_id)
    return AreaFact(value=area, prov=prov, set_at_turn=turn)


async def apply_memory(deps: RunDeps, errand: Errand, *, fresh: bool, turn: int) -> Errand:
    """The errand with stale memory fields cleared and global pre-fill applied."""
    async with deps.db.session() as repos:
        rows = {r.memory_id: r for r in await repos.memories.for_user(deps.user_id)}
    eligible = {mid for mid, r in rows.items() if r.status == "active"}
    brief: Brief = errand.brief
    updates: dict[str, Any] = {}

    for name in stale_fields(brief, eligible):
        fact = getattr(brief, name)
        old_id = fact.prov.memory_id
        successor = _active_successor(rows, old_id)
        reason = "memory_superseded" if successor is not None else "memory_forgotten"
        deps.tracer.emit(
            "invalidate",
            what={"brief_field": name, "memory_id": old_id},
            reason=reason,
        )
        updates[name] = None
        if name == "area" and successor is not None and successor.key == "home_area":
            updates[name] = _area_from(successor, turn)
    cleared, _ = scrub_brief(brief, eligible)
    if cleared is not brief or updates:
        brief = cleared.model_copy(update=updates)

    if fresh and brief.area is None and errand.kind in (ErrandKind.BOOK, ErrandKind.REBOOK):
        home = next(
            (
                r
                for r in rows.values()
                if r.status == "active" and r.key == "home_area" and r.scope == GLOBAL
            ),
            None,
        )
        fact = _area_from(home, turn) if home is not None else None
        if fact is not None:
            brief = brief.model_copy(update={"area": fact})
    if brief is errand.brief:
        return errand
    return errand.model_copy(
        update={"brief": brief.model_copy(update={"version": brief.version + 1})}
    )
