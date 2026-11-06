"""Code resolves time expressions; the model never writes a date (10.5, ADR 0015).

M3 supports a deliberately closed set of phrases: today, tomorrow, this week and next
week, plus `part_of_day` and `not_before` / `not_after`. Anything else is
`Unresolved`, which the planner gets back as a tool error so it asks the user. M8
owns the full resolver (weekday names, "this weekend", ...) behind this same
signature, with its unit tests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from errand.domain.brief import TimeWindow, TimeWindowExpr
from errand.domain.windows import fmt_day, slot_fits, slot_label
from errand.world.contracts import MAX_RANGE_DAYS, DateRange

__all__ = ["Unresolved", "date_range", "resolve", "slot_fits", "slot_label"]

# Part-of-day bounds from 10.5 ("mornings" is 07:00 to 12:00).
PARTS_OF_DAY: dict[str, tuple[time, time]] = {
    "morning": (time(7), time(12)),
    "afternoon": (time(12), time(17)),
    "evening": (time(17), time(21)),
}
SUPPORTED_PHRASES = ("today", "tomorrow", "this week", "next week")


@dataclass(frozen=True)
class Unresolved:
    phrase: str
    reason: str


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip()


def _days(phrase: str, today: date) -> tuple[date, date] | None:
    """Inclusive first and last day for a supported phrase."""
    if phrase == "today":
        return today, today
    if phrase == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    if phrase == "this week":
        return today, monday + timedelta(days=6)
    if phrase == "next week":
        start = monday + timedelta(days=7)
        return start, start + timedelta(days=6)
    return None


_fmt_day = fmt_day


def _label(first: date, last: date, daily_from: time | None, daily_to: time | None) -> str:
    days = _fmt_day(first) if first == last else f"{_fmt_day(first)} - {_fmt_day(last)}"
    if daily_from is None and daily_to is None:
        return days
    lo = (daily_from or time(0)).strftime("%H:%M")
    hi = (daily_to or time(23, 59)).strftime("%H:%M")
    return f"{days}, {lo}-{hi}"


def resolve(expr: TimeWindowExpr, now: datetime, tz: tzinfo) -> TimeWindow | Unresolved:
    phrase = _norm(expr.phrase)
    local_now = now.astimezone(tz)
    days = _days(phrase, local_now.date())
    if days is None:
        return Unresolved(
            expr.phrase,
            f"cannot resolve {expr.phrase!r} yet; supported: {', '.join(SUPPORTED_PHRASES)}",
        )
    first, last = days
    daily_from: time | None = None
    daily_to: time | None = None
    if expr.part_of_day is not None and expr.part_of_day != "any":
        daily_from, daily_to = PARTS_OF_DAY[expr.part_of_day]
    if expr.not_before is not None:
        daily_from = max(daily_from or time(0), expr.not_before)
    if expr.not_after is not None:
        daily_to = min(daily_to or time(23, 59), expr.not_after)
    if daily_from is not None and daily_to is not None and daily_from >= daily_to:
        return Unresolved(expr.phrase, "the daily window is empty")
    start = max(local_now, datetime.combine(first, time(0), tz))
    end = datetime.combine(last + timedelta(days=1), time(0), tz)
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        return Unresolved(expr.phrase, f"covers more than {MAX_RANGE_DAYS} days")
    return TimeWindow(
        start=start,
        end=end,
        daily_from=daily_from,
        daily_to=daily_to,
        weekdays=None,
        label=_label(first, last, daily_from, daily_to),
    )


def date_range(window: TimeWindow, tz: tzinfo) -> DateRange:
    """The search `date_range` for a window: its calendar days, both inclusive."""
    first = window.start.astimezone(tz).date()
    last = (window.end.astimezone(tz) - timedelta(microseconds=1)).date()
    return DateRange.model_validate({"from": first, "to": last})
