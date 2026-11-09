"""Replacement search rules (architecture 14.9 to 14.12, Gate 3 decisions 9 to 12).

The window and the price ceiling are pure. `build_shortlist` turns a search answer into the
ranked shortlist with the normal filters (`tools.views.hard_filter`) and the normal code
ranking (`agent.ranking`), with the cancelling provider sorted after every other fit and
`review_signal` 0 (no reader call in a follow-up run, decision 11). Nothing here calls a
tool or a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

from errand.agent import ranking
from errand.domain.brief import TimeWindow
from errand.domain.slots import Exclusion, Shortlist
from errand.tools import views
from errand.world.contracts import SearchProvidersOut

# The window is the original day plus or minus this many days.
WINDOW_DAYS = 2
# No slot earlier than this long after now: a replacement nobody can reach is no offer.
LEAD = timedelta(hours=2)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


def _midnight(moment: datetime, tz: tzinfo) -> datetime:
    local = moment.astimezone(tz)
    return datetime.combine(local.date(), time(0, 0), tzinfo=tz)


def window_end(slot_start: datetime, tz: tzinfo) -> datetime:
    """The end of the replacement window: the original day plus two, to the end of that day.
    When the clock passes it, a `detected` or `notified` job lapses (decision 16)."""
    return _midnight(slot_start, tz) + timedelta(days=WINDOW_DAYS + 1)


def replacement_window(
    slot_start: datetime,
    now: datetime,
    tz: tzinfo,
    *,
    urgent: bool = False,
    reschedule_target: datetime | None = None,
) -> Interval | None:
    """The days to search: the original day +/- 2, starting no earlier than now + 2 h.

    Urgent (the slot is within 24 h): today and tomorrow only. A reschedule card that was
    pending or granted moves the centre to the day it was moving to. None when nothing of
    the window is left.
    """
    centre = reschedule_target or slot_start
    start = max(_midnight(centre, tz) - timedelta(days=WINDOW_DAYS), now + LEAD)
    end = window_end(centre, tz)
    if urgent:
        end = min(end, _midnight(now, tz) + timedelta(days=2))
    if start >= end:
        return None
    return Interval(start, end)


Budget = int | Literal["none"] | None


def price_ceiling(
    budget_ceiling: int | None, original_budget: Budget, approved_price: int | None
) -> int | None:
    """The first that exists: the current `budget_ceiling` for the service, the original
    brief's numeric budget, the price the user approved for the cancelled booking. A budget
    of "none" falls through to the approved price (decision 10), so an offer nobody asked
    for never costs more than what was agreed. None means no ceiling can be derived."""
    if budget_ceiling is not None:
        return budget_ceiling
    if isinstance(original_budget, int) and not isinstance(original_budget, bool):
        return original_budget
    return approved_price


@dataclass(frozen=True)
class Built:
    shortlist: Shortlist
    # The cheapest price among providers that fit everything but the ceiling, for the
    # "Up to ₹N" button; None when no provider is only over the ceiling.
    cheapest_over_ceiling: int | None


def build_shortlist(
    result: SearchProvidersOut,
    *,
    window: TimeWindow,
    ceiling: int | None,
    avoid: frozenset[str],
    cancelling_provider: str,
    brief_version: int,
    tz: tzinfo,
) -> Built:
    budget: Budget = ceiling
    eligible, excluded = views.hard_filter(result, window=window, budget=budget, tz=tz)
    kept = [e for e in eligible if e.summary.provider_id not in avoid]
    excluded = [
        *excluded,
        *(
            Exclusion(provider_id=e.summary.provider_id, name=e.name, reason="avoided")
            for e in eligible
            if e.summary.provider_id in avoid
        ),
    ]
    inputs = {
        e.summary.provider_id: ranking.RankInput(
            provider_id=e.summary.provider_id,
            rating=e.summary.rating,
            price_inr=e.summary.price_inr,
            fitting_slots=len(e.fitting),
        )
        for e in kept
    }
    ranked = ranking.rank(list(inputs.values()), budget=budget)
    # The cancelling provider is not excluded, only sorted after every other fit (14.4).
    ranked = [r for r in ranked if r.provider_id != cancelling_provider] + [
        r for r in ranked if r.provider_id == cancelling_provider
    ]
    by_id = {e.summary.provider_id: e for e in kept}
    placed = [views.Placed(by_id[r.provider_id], r.score, r.parts, digest=None) for r in ranked]
    shortlist = views.assemble_shortlist(
        placed, excluded, brief_version=brief_version, window_label=window.label
    )
    cheapest = None
    if ceiling is not None:
        everything, _ = views.hard_filter(result, window=window, budget=None, tz=tz)
        over = [
            e.summary.price_inr
            for e in everything
            if e.summary.provider_id not in avoid and e.summary.price_inr > ceiling
        ]
        cheapest = min(over) if over else None
    return Built(shortlist, cheapest)
