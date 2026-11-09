"""Memory in a brief, as one pure function (gate 2 decisions 10, 11 and 17; gate 3 decision 8).

Two callers use it and neither keeps a second copy of the rules: `agent.memory_fill` at the
start of every run (it reads the rows and writes the `invalidate` trace records) and
`followup.brief` when a replacement brief is cloned from the original errand. Given the
brief, the user's memory rows, now, the zone and the turn it returns the new brief and the
invalidations that explain what changed:

1. **Clear.** A field whose `prov.memory_id` is no longer eligible is cleared. A corrected
   row refills the field from its active successor (an area from `home_area`, a window
   bound from `earliest_time` or `latest_time`).
2. **Drop stale window bounds.** A time bound whose row was forgotten leaves the window, or
   takes the successor's value; the user's own day stays (13.7).
3. **Pre-fill.** With `fresh`, a missing area of a booking errand comes from the active
   global `home_area`.

It imports neither the agent nor the API: the zone and the clock come in as arguments.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, tzinfo
from typing import Any

from errand.domain import timewin
from errand.domain.brief import AreaFact, Brief, ErrandKind, Provenance, WindowFact
from errand.memory.registry import GLOBAL, RegistryError, spec_for
from errand.memory.scrub import scrub_brief, stale_fields
from errand.store.repos.memories import MemoryRow
from errand.world.contracts import Area


@dataclass(frozen=True)
class Invalidation:
    """One `invalidate` trace record's fields: what was cleared and why."""

    what: dict[str, str]
    reason: str


@dataclass(frozen=True)
class Applied:
    brief: Brief  # the same object when nothing changed
    invalidations: tuple[Invalidation, ...]


def clock_time(value: object) -> time | None:
    """A stored "HH:MM" as a time of day, or None when it does not parse."""
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        return None


def active_successor(rows: dict[str, MemoryRow], memory_id: str) -> MemoryRow | None:
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


def _drop_stale_bounds(
    brief: Brief,
    rows: dict[str, MemoryRow],
    eligible: set[str],
    *,
    now: datetime,
    tz: tzinfo,
    out: list[Invalidation],
) -> Brief:
    """A time bound whose row was forgotten or corrected leaves the window, or is replaced
    by the active successor's value; the user's own day stays (13.7)."""
    window = brief.window
    if window is None or window.expr is None or not window.bound_memory_ids:
        return brief
    stale = {k: m for k, m in window.bound_memory_ids.items() if m not in eligible}
    if not stale:
        return brief
    owners = {k: m for k, m in window.bound_memory_ids.items() if k not in stale}
    changes: dict[str, Any] = dict.fromkeys(stale)
    for bound, old_id in stale.items():
        successor = active_successor(rows, old_id)
        value = clock_time(successor.value) if successor is not None else None
        if successor is not None and value is not None:
            changes[bound], owners[bound] = value, successor.memory_id
        out.append(
            Invalidation(
                {"brief_field": f"window.{bound}", "memory_id": old_id},
                "memory_superseded" if successor is not None else "memory_forgotten",
            )
        )
    expr = window.expr.model_copy(update=changes)
    resolved = timewin.resolve(expr, now, tz)
    if isinstance(resolved, timewin.Unresolved):
        return brief  # the pure scrub then clears the window whole
    fact = window.model_copy(update={"value": resolved, "expr": expr, "bound_memory_ids": owners})
    return brief.model_copy(update={"window": fact})


def apply_memory(
    brief: Brief,
    rows: Iterable[MemoryRow],
    *,
    kind: ErrandKind,
    fresh: bool,
    now: datetime,
    tz: tzinfo,
    turn: int,
) -> Applied:
    """The brief with stale memory fields cleared and global pre-fill applied."""
    by_id = {r.memory_id: r for r in rows}
    eligible = {mid for mid, r in by_id.items() if r.status == "active"}
    invalidations: list[Invalidation] = []
    current = _drop_stale_bounds(brief, by_id, eligible, now=now, tz=tz, out=invalidations)
    updates: dict[str, Any] = {}

    for name in stale_fields(current, eligible):
        fact = getattr(current, name)
        if name == "provider_pref":  # a component leaves, the other stays (scrub_brief below)
            invalidations.append(Invalidation({"brief_field": name}, "memory_forgotten"))
            continue
        old_id = fact.prov.memory_id
        successor = active_successor(by_id, old_id)
        invalidations.append(
            Invalidation(
                {"brief_field": name, "memory_id": old_id},
                "memory_superseded" if successor is not None else "memory_forgotten",
            )
        )
        updates[name] = None
        if name == "area" and successor is not None and successor.key == "home_area":
            updates[name] = _area_from(successor, turn)
    cleared, _ = scrub_brief(current, eligible)
    if cleared is not current or updates:
        current = cleared.model_copy(update=updates)

    if fresh and current.area is None and kind in (ErrandKind.BOOK, ErrandKind.REBOOK):
        home = next(
            (
                r
                for r in by_id.values()
                if r.status == "active" and r.key == "home_area" and r.scope == GLOBAL
            ),
            None,
        )
        fact = _area_from(home, turn) if home is not None else None
        if fact is not None:
            current = current.model_copy(update={"area": fact})
    return Applied(current if current is not brief else brief, tuple(invalidations))


def apply_bounds(
    window: WindowFact,
    earliest: tuple[str, object] | None,
    latest: tuple[str, object] | None,
    *,
    now: datetime,
    tz: tzinfo,
) -> tuple[WindowFact | None, bool]:
    """The window with the selected `earliest_time` and `latest_time` rows applied as
    `not_before` and `not_after` where the user's own expression left them unset.

    Each argument is `(memory_id, stored value)` of the row selection picked, or None. A
    bound code applied from memory earlier is ours to replace; the user's is not. Returns
    the new window (None when nothing changed or the expression no longer resolves) and
    whether a bound was newly filled. The window records each bound's row in
    `bound_memory_ids`, apart from `prov` (the user's day).
    """
    expr = window.expr
    if expr is None:
        return None, False
    base = expr.model_copy(update={k: None for k in window.bound_memory_ids})
    changes: dict[str, object] = {}
    owners: dict[str, str] = {}
    for bound, row, have in (
        ("not_before", earliest, base.not_before),
        ("not_after", latest, base.not_after),
    ):
        if have is None and row is not None and (t := clock_time(row[1])) is not None:
            changes[bound], owners[bound] = t, row[0]
    new_expr = base.model_copy(update=changes)
    if new_expr == expr and owners == window.bound_memory_ids:
        return None, False
    resolved = timewin.resolve(new_expr, now, tz)
    if isinstance(resolved, timewin.Unresolved):
        return None, False
    fact = WindowFact(
        value=resolved,
        expr=new_expr,
        prov=window.prov,
        set_at_turn=window.set_at_turn,
        bound_memory_ids=owners,
    )
    return fact, bool(changes)
