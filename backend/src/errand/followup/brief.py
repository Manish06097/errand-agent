"""The replacement brief (architecture 14.4, Gate 3 decision 8).

The original errand's brief is cloned into a REBOOK errand: what the user stated stays,
the window becomes the replacement window and the budget becomes the price ceiling
(decision 10). A booking with no origin brief (a seeded one, or one made elsewhere) gets a
brief built from the booking itself: its service and day. With no area known there is no
search, and the notice asks where to look. Re-resolving memory-sourced fields through
`apply_memory` is N2's piece (TODO N2: call it here, after the clone).
"""

from __future__ import annotations

from datetime import timedelta, tzinfo

from errand.domain.brief import (
    Brief,
    BudgetFact,
    Provenance,
    ServiceFact,
    TextFact,
    TimeWindow,
    WindowFact,
)
from errand.domain.windows import fmt_day
from errand.followup.search import Interval, price_ceiling
from errand.store.repos.known_bookings import KnownBooking
from errand.world.contracts import Service


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


def replacement_brief(
    known: KnownBooking,
    *,
    interval: Interval,
    tz: tzinfo,
    approved_price: int | None,
) -> Brief:
    origin = (
        Brief.model_validate_json(known.origin_brief_json) if known.origin_brief_json else Brief()
    )
    original_window = origin.window.value if origin.window else None
    window = TimeWindow(
        start=interval.start,
        end=interval.end,
        daily_from=original_window.daily_from if original_window else None,
        daily_to=original_window.daily_to if original_window else None,
        weekdays=original_window.weekdays if original_window else None,
        label=window_label(interval, tz, part_of_day=part_of_day_words(original_window)),
    )
    original_budget = origin.budget_max_inr.value if origin.budget_max_inr else None
    ceiling = price_ceiling(None, original_budget, approved_price)  # TODO N2: budget_ceiling
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
        provider_pref=origin.provider_pref,
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
