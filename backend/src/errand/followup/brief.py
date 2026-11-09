"""The replacement brief (architecture 14.4, Gate 3 decision 8).

The original errand's brief is cloned into a REBOOK errand: what the user stated stays,
the window becomes the replacement window and the budget becomes the price ceiling
(decision 10). A booking with no origin brief (a seeded one, or one made elsewhere) gets a
brief built from the booking itself: its service and day. With no area known there is no
search, and the notice asks where to look.

Memory is re-read, not copied (decision 8): the same pure `apply_memory` the run start uses
clears what was forgotten and refills what was corrected; the selected rows for the service
then supply the budget ceiling (decision 10), the time bounds and the provider preference
the user changed since booking. What the user stated themselves stays. The original
window's phrase ("tomorrow") belongs to the day it was said, so only its time-of-day bounds
are re-resolved, on a neutral phrase, and the days come from the replacement window.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo

from errand.clock import to_ms
from errand.config import MemorySettings
from errand.context.selection import Chosen, select
from errand.domain.brief import (
    Brief,
    BudgetFact,
    ErrandKind,
    Provenance,
    ProviderPref,
    ProviderPrefFact,
    ServiceFact,
    TextFact,
    TimeWindow,
    WindowFact,
)
from errand.domain.windows import fmt_day
from errand.followup.search import Interval, price_ceiling
from errand.memory import apply
from errand.store.repos.known_bookings import KnownBooking
from errand.store.repos.memories import MemoryRow
from errand.world.contracts import Service

# A phrase that always resolves and never depends on the clock: the time-of-day bounds of
# an expression do not depend on its days.
_NEUTRAL_PHRASE = "next week"


def _prov(booking_id: str) -> Provenance:
    return Provenance(source="followup", booking_id=booking_id)


def window_label(interval: Interval, tz: tzinfo, *, part_of_day: str = "") -> str:
    first = interval.start.astimezone(tz).date()
    last = (interval.end - timedelta(microseconds=1)).astimezone(tz).date()
    span = fmt_day(first) if first == last else f"{fmt_day(first)} to {fmt_day(last)}"
    return f"{span}{part_of_day}"


def part_of_day_words(window: TimeWindow | None) -> str:
    if window is None or (window.daily_from is None and window.daily_to is None):
        return ""
    lo = f"{window.daily_from:%H:%M}" if window.daily_from else "00:00"
    hi = f"{window.daily_to:%H:%M}" if window.daily_to else "24:00"
    return f", {lo}-{hi}"


def current_rows(rows: Sequence[MemoryRow], *, service: str, now: datetime) -> dict[str, Chosen]:
    """The selected row per key for this service, shadowing applied, by key."""
    selection = select(
        rows,
        errand_kind=ErrandKind.REBOOK.value,
        service=service,
        now_ms=to_ms(now),
        half_life_days=MemorySettings().half_life_days,
        cap_tokens=10**9,
        measure=lambda _: 0,
    )
    return {c.row.key: c for c in selection.included}


def _pref(origin: Brief, chosen: dict[str, Chosen]) -> ProviderPrefFact | None:
    """A preference the user stated stays; one that came from memory follows memory now."""
    pref = origin.provider_pref
    if pref is not None and pref.prov.source != "memory":
        return pref
    prefer, avoid = chosen.get("preferred_provider"), chosen.get("avoid_provider")
    if prefer is None and avoid is None:
        return None
    return ProviderPrefFact(
        value=ProviderPref(
            prefer_id=str(prefer.value) if prefer is not None else None,
            avoid_ids=list(avoid.value) if avoid is not None else [],
        ),
        prov=Provenance(source="memory"),
        set_at_turn=0,
        prefer_memory_id=prefer.memory_id if prefer is not None else None,
        avoid_memory_id=avoid.memory_id if avoid is not None else None,
    )


def _daily_bounds(
    window: WindowFact,
    chosen: dict[str, Chosen],
    rows: Sequence[MemoryRow],
    *,
    now: datetime,
    tz: tzinfo,
) -> WindowFact:
    """The original window with its time-of-day bounds re-read from memory."""
    if window.expr is None:
        return window
    neutral = window.model_copy(
        update={"expr": window.expr.model_copy(update={"phrase": _NEUTRAL_PHRASE})}
    )
    cleared = apply.apply_memory(
        Brief(window=neutral), rows, kind=ErrandKind.REBOOK, fresh=False, now=now, tz=tz, turn=0
    ).brief.window
    if cleared is None:  # the bound's row is gone and the rest no longer resolves
        return window.model_copy(
            update={"value": window.value.model_copy(update={"daily_from": None, "daily_to": None})}
        )
    earliest, latest = chosen.get("earliest_time"), chosen.get("latest_time")
    fresh, _ = apply.apply_bounds(
        cleared,
        (earliest.memory_id, earliest.value) if earliest is not None else None,
        (latest.memory_id, latest.value) if latest is not None else None,
        now=now,
        tz=tz,
    )
    return fresh or cleared


def replacement_brief(
    known: KnownBooking,
    *,
    interval: Interval,
    tz: tzinfo,
    now: datetime,
    approved_price: int | None,
    rows: Sequence[MemoryRow] = (),
) -> Brief:
    origin = (
        Brief.model_validate_json(known.origin_brief_json) if known.origin_brief_json else Brief()
    )
    origin = apply.apply_memory(
        origin, rows, kind=ErrandKind.REBOOK, fresh=True, now=now, tz=tz, turn=0
    ).brief
    chosen = current_rows(rows, service=known.service, now=now)
    original_fact = origin.window
    bounded = _daily_bounds(original_fact, chosen, rows, now=now, tz=tz) if original_fact else None
    original_window = bounded.value if bounded else None
    window = TimeWindow(
        start=interval.start,
        end=interval.end,
        daily_from=original_window.daily_from if original_window else None,
        daily_to=original_window.daily_to if original_window else None,
        weekdays=original_fact.value.weekdays if original_fact else None,
        label=window_label(interval, tz, part_of_day=part_of_day_words(original_window)),
    )
    original_budget = origin.budget_max_inr.value if origin.budget_max_inr else None
    current_budget = chosen.get("budget_ceiling")
    ceiling = price_ceiling(
        current_budget.value if current_budget and isinstance(current_budget.value, int) else None,
        original_budget,
        approved_price,
    )
    prov = _prov(known.booking_id)
    service = origin.service or ServiceFact(value=Service(known.service), prov=prov, set_at_turn=0)
    return Brief(
        version=1,
        service=service,
        issue=origin.issue,
        area=origin.area,
        window=WindowFact(value=window, prov=prov, set_at_turn=0),
        budget_max_inr=None
        if ceiling is None
        else BudgetFact(value=ceiling, prov=prov, set_at_turn=0),
        provider_pref=_pref(origin, chosen),
        target_booking_id=TextFact(value=known.booking_id, prov=prov, set_at_turn=0),
    )


def widened(brief: Brief, field: str, value: str | None, *, tz: tzinfo, booking_id: str) -> Brief:
    """One ui_action brief patch for this errand only (decision 13); never a memory write."""
    prov = Provenance(source="ui_action", booking_id=booking_id)
    window = brief.window
    if field == "days" and window is not None:
        w = window.value
        end = w.end + timedelta(days=7)
        label = window_label(Interval(w.start, end), tz, part_of_day=part_of_day_words(w))
        patched = w.model_copy(update={"end": end, "label": label})
        return brief.model_copy(
            update={
                "version": brief.version + 1,
                "window": window.model_copy(update={"value": patched}),
            }
        )
    if field == "time" and window is not None:
        w = window.value
        label = window_label(Interval(w.start, w.end), tz)
        patched = w.model_copy(
            update={"daily_from": None, "daily_to": None, "weekdays": None, "label": label}
        )
        return brief.model_copy(
            update={
                "version": brief.version + 1,
                "window": window.model_copy(update={"value": patched}),
            }
        )
    if field == "price" and value is not None and value.isdigit() and int(value) > 0:
        fact = BudgetFact(value=int(value), prov=prov, set_at_turn=0)
        return brief.model_copy(update={"version": brief.version + 1, "budget_max_inr": fact})
    return brief
