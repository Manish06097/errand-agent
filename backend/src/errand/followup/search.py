"""Replacement search rules (architecture 14.9 to 14.12, Gate 3 decisions 9 to 12).

The pure parts live here: the window and the price ceiling. Filters and ranking are the
normal ones (`agent/views.hard_filter`, `agent/ranking`), and the search itself is added
with the follow-up node. Nothing here calls a tool or a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

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
