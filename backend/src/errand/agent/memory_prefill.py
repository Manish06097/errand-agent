"""Service-scoped pre-fill inside `update_brief`'s merge (gate 2 decision 17, N2B 2.4).

Global keys fill in `ingress` (`memory_fill`). The service is not known there, so the keys
scoped to a service fill here, after a patch sets or changes the service or the window:

- `budget_ceiling` fills `budget_max_inr` when the brief has none (and the user has not
  cleared it in this errand), with `prov.source = "memory"` and `prov.memory_id`. A budget
  that came from another service's memory is replaced.
- `earliest_time` and `latest_time` (global or service-scoped; selection picks the row, so
  an electrician 10:00 shadows a global 08:00, the same rule the manifest reports) apply to
  the window as `not_before` and `not_after` when the user's own expression left them
  unset. The window records each bound's row in `bound_memory_ids`, apart from `prov`
  (the user's day), so a forget drops the bound and keeps the day. A bound never makes
  the window count as filled: only the user's day does.
- `preferred_provider` and `avoid_provider` feed `provider_pref` (`prefer_id`, `avoid_ids`)
  with source `memory`, each component with its own row id (`prefer_memory_id`,
  `avoid_memory_id`) so forgetting one clears only that one; `brief_ops.pinned` only pins on
  `booking_history`, so a preference ranks and never pins. `avoid_ids` reaches the hard
  filter in code.

The current message beats memory: a bound or budget the user set stays. `language`,
`access_note` and `avoid_days` feed nothing in code here (avoid_days is an open item).
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from errand.agent.deps import RunDeps
from errand.clock import to_ms
from errand.config import MemorySettings
from errand.context.selection import Chosen, select
from errand.domain.brief import (
    Brief,
    BriefField,
    BudgetFact,
    Provenance,
    ProviderPref,
    ProviderPrefFact,
)
from errand.memory.apply import apply_bounds


async def chosen_rows(deps: RunDeps, errand_kind: str, service: str | None) -> dict[str, Chosen]:
    """The selected row per key for this service (shadowing applied), by key."""
    async with deps.db.session() as repos:
        rows = await repos.memories.for_user(deps.user_id)
    selection = select(
        rows,
        errand_kind=errand_kind,
        service=service,
        now_ms=to_ms(deps.clock.now()),
        half_life_days=MemorySettings().half_life_days,
        cap_tokens=10**9,
        measure=lambda _: 0,
    )
    return {c.row.key: c for c in selection.included}


async def apply_service_prefill(
    deps: RunDeps,
    brief: Brief,
    *,
    errand_kind: str,
    set_fields: list[BriefField],
    turn: int,
    now: datetime,
    tz: tzinfo,
) -> tuple[Brief, list[BriefField]]:
    """The brief with service-scoped memory applied, and the fields memory filled."""
    if brief.service is None:
        return brief, []
    service_set = BriefField.SERVICE in set_fields
    window_set = BriefField.WINDOW in set_fields
    if not (service_set or window_set):
        return brief, []
    chosen = await chosen_rows(deps, errand_kind, brief.service.value.value)
    filled: list[BriefField] = []
    update: dict[str, object] = {}

    budget = chosen.get("budget_ceiling")
    current = brief.budget_max_inr
    from_memory = current is not None and current.prov.source == "memory"
    if (
        service_set
        and BriefField.BUDGET not in brief.user_cleared
        and (current is None or from_memory)
    ):
        if budget is not None and isinstance(budget.value, int):
            update["budget_max_inr"] = BudgetFact(
                value=budget.value,
                prov=Provenance(source="memory", memory_id=budget.memory_id),
                set_at_turn=turn,
            )
            filled.append(BriefField.BUDGET)
        elif from_memory:
            update["budget_max_inr"] = None

    window = brief.window
    if window is not None and window.expr is not None and (service_set or window_set):
        earliest, latest = chosen.get("earliest_time"), chosen.get("latest_time")
        fact, newly = apply_bounds(
            window,
            (earliest.memory_id, earliest.value) if earliest is not None else None,
            (latest.memory_id, latest.value) if latest is not None else None,
            now=now,
            tz=tz,
        )
        if fact is not None:
            update["window"] = fact
            if newly:
                filled.append(BriefField.WINDOW)

    if service_set:
        prefer, avoid = chosen.get("preferred_provider"), chosen.get("avoid_provider")
        pref = brief.provider_pref
        if (prefer is not None or avoid is not None) and (
            pref is None or pref.prov.source == "memory"
        ):
            update["provider_pref"] = ProviderPrefFact(
                value=ProviderPref(
                    prefer_id=str(prefer.value) if prefer is not None else None,
                    avoid_ids=list(avoid.value) if avoid is not None else [],
                ),
                prov=Provenance(source="memory"),
                set_at_turn=turn,
                prefer_memory_id=prefer.memory_id if prefer is not None else None,
                avoid_memory_id=avoid.memory_id if avoid is not None else None,
            )
    if not update:
        return brief, []
    return brief.model_copy(update=update), filled
