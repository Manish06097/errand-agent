"""Code resolves time expressions; the model never writes a date (10.5, ADR 0015).

The closed set (M8): today, tomorrow, this week, next week, this weekend, next weekend,
a weekday name (full or three letters), "this <weekday>", "next <weekday>", and weekday
lists joined by "or", "and" or commas ("Thursday or Friday"). `TimeWindowExpr` has no
`days` field: day names travel in `phrase` and code resolves them here. Anything else,
a day of the month included, is `Unresolved`, which the planner gets back as a tool
error naming the supported list, so it asks the user.

Rulings (architecture 10.5, "M8 rulings"):
- weeks start Monday; a bare weekday is its next occurrence, today included while any
  part of the daily window is still ahead of now;
- "this <weekday>" is that day in the current Monday-Sunday week, and Unresolved once it
  has passed; "next <weekday>" is that day in next calendar week, as "next week" is;
- "this weekend" is Saturday and Sunday of this week (today only on a Sunday), "next
  weekend" is next week's;
- a weekday list sets `TimeWindow.weekdays` (ISO numbers, 1 = Monday), which
  `domain.windows.slot_fits` reads; start and end span the first to the last listed day.

The signature is unchanged since M3: `resolve(expr, now, tz)`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from errand.config import DayPartSettings
from errand.domain.brief import TimeWindow, TimeWindowExpr
from errand.domain.windows import fmt_day, slot_fits, slot_label
from errand.world.contracts import MAX_RANGE_DAYS, DateRange

__all__ = ["Unresolved", "date_range", "resolve", "slot_fits", "slot_label"]

SUPPORTED_PHRASES = (
    "today",
    "tomorrow",
    "this week",
    "next week",
    "this weekend",
    "next weekend",
    "a weekday name (Thursday, Thu)",
    "this <weekday>",
    "next <weekday>",
    "weekday lists (Thursday or Friday)",
)

_WEEKDAYS: dict[str, int] = {}
for _i, (_full, _short) in enumerate(
    [
        ("monday", "mon"),
        ("tuesday", "tue"),
        ("wednesday", "wed"),
        ("thursday", "thu"),
        ("friday", "fri"),
        ("saturday", "sat"),
        ("sunday", "sun"),
    ],
    start=1,
):
    _WEEKDAYS[_full] = _i
    _WEEKDAYS[_short] = _i
_LIST_SPLIT = re.compile(r"\s*,\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+")


@dataclass(frozen=True)
class Unresolved:
    phrase: str
    reason: str
    empty_daily: bool = False


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip(" .!?")


def _label(
    dates: list[date], contiguous: bool, daily_from: time | None, daily_to: time | None
) -> str:
    if len(dates) == 1:
        days = fmt_day(dates[0])
    elif contiguous:
        days = f"{fmt_day(dates[0])} - {fmt_day(dates[-1])}"
    else:
        days = " or ".join(fmt_day(d) for d in dates)
    if daily_from is None and daily_to is None:
        return days
    if daily_from is not None and daily_to is not None:
        return f"{days}, {daily_from:%H:%M}-{daily_to:%H:%M}"
    if daily_from is not None:
        return f"{days}, from {daily_from:%H:%M}"
    return f"{days}, until {daily_to:%H:%M}"


class _Phrase:
    """A phrase resolved to dates: the days it names, and whether to filter by weekday."""

    def __init__(self, dates: list[date], weekdays: frozenset[int] | None = None) -> None:
        self.dates = sorted(dates)
        self.weekdays = weekdays

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    @property
    def contiguous(self) -> bool:
        return (self.last - self.first).days + 1 == len(self.dates)


def _daily_window(expr: TimeWindowExpr, parts: DayPartSettings) -> tuple[time | None, time | None]:
    daily_from: time | None = None
    daily_to: time | None = None
    if expr.part_of_day is not None and expr.part_of_day != "any":
        daily_from, daily_to = parts.bounds()[expr.part_of_day]
    if expr.not_before is not None:
        daily_from = max(daily_from or time(0), expr.not_before)
    if expr.not_after is not None:
        daily_to = min(daily_to or time(23, 59), expr.not_after)
    return daily_from, daily_to


def _open_today(local_now: datetime, daily_to: time | None) -> bool:
    """Is any part of today's daily window still ahead of now?"""
    return daily_to is None or local_now.time() < daily_to


def _dates_for(
    phrase: str, local_now: datetime, daily_to: time | None
) -> _Phrase | Unresolved | None:
    """The days a phrase names, None if it is not in the closed set."""
    today = local_now.date()
    monday = today - timedelta(days=today.weekday())
    if phrase == "today":
        return _Phrase([today])
    if phrase == "tomorrow":
        return _Phrase([today + timedelta(days=1)])
    if phrase == "this week":
        return _Phrase([today + timedelta(days=i) for i in range(7 - today.weekday())])
    if phrase == "next week":
        return _Phrase([monday + timedelta(days=7 + i) for i in range(7)])
    if phrase in ("this weekend", "next weekend"):
        base = monday if phrase == "this weekend" else monday + timedelta(days=7)
        days = [base + timedelta(days=5), base + timedelta(days=6)]
        if phrase == "this weekend":
            days = [
                d for d in days if d > today or (d == today and _open_today(local_now, daily_to))
            ]
            if not days:
                return Unresolved(phrase, "this weekend has already passed")
        return _Phrase(days)
    bare = _WEEKDAYS.get(phrase)
    if bare is not None:
        return _Phrase([_next_occurrence(bare, local_now, daily_to)])
    qualifier, _, name = phrase.partition(" ")
    named = _WEEKDAYS.get(name)
    if qualifier in ("this", "next") and named is not None:
        if qualifier == "next":
            return _Phrase([monday + timedelta(days=7 + named - 1)])
        day = monday + timedelta(days=named - 1)
        if day < today or (day == today and not _open_today(local_now, daily_to)):
            return Unresolved(phrase, f"{phrase} has already passed; ask which day they mean")
        return _Phrase([day])
    names = [part for part in _LIST_SPLIT.split(phrase) if part]
    if len(names) >= 2 and all(n in _WEEKDAYS for n in names):
        numbers = {_WEEKDAYS[n] for n in names}
        dates = [_next_occurrence(n, local_now, daily_to) for n in sorted(numbers)]
        weekdays = frozenset(numbers) if len(numbers) > 1 else None
        return _Phrase(dates, weekdays)
    return None


def _next_occurrence(weekday: int, local_now: datetime, daily_to: time | None) -> date:
    today = local_now.date()
    ahead = (weekday - 1 - today.weekday()) % 7
    if ahead == 0 and not _open_today(local_now, daily_to):
        ahead = 7
    return today + timedelta(days=ahead)


def resolve(
    expr: TimeWindowExpr, now: datetime, tz: tzinfo, parts: DayPartSettings | None = None
) -> TimeWindow | Unresolved:
    parts = parts or DayPartSettings()
    phrase = _norm(expr.phrase)
    if phrase.startswith("on "):
        phrase = phrase[3:]
    local_now = now.astimezone(tz)
    daily_from, daily_to = _daily_window(expr, parts)
    if daily_from is not None and daily_to is not None and daily_from >= daily_to:
        return Unresolved(expr.phrase, "the daily window is empty", empty_daily=True)
    found = _dates_for(phrase, local_now, daily_to)
    if found is None:
        return Unresolved(
            expr.phrase,
            f"cannot resolve {expr.phrase!r}; supported: {', '.join(SUPPORTED_PHRASES)}",
        )
    if isinstance(found, Unresolved):
        return Unresolved(expr.phrase, found.reason)
    if (found.last - found.first).days + 1 > MAX_RANGE_DAYS:
        return Unresolved(expr.phrase, f"covers more than {MAX_RANGE_DAYS} days")
    start = max(local_now, datetime.combine(found.first, time(0), tz))
    end = datetime.combine(found.last + timedelta(days=1), time(0), tz)
    return TimeWindow(
        start=start,
        end=end,
        daily_from=daily_from,
        daily_to=daily_to,
        weekdays=found.weekdays,
        label=_label(found.dates, found.contiguous, daily_from, daily_to),
    )


def date_range(window: TimeWindow, tz: tzinfo) -> DateRange:
    """The search `date_range` for a window: its calendar days, both inclusive."""
    first = window.start.astimezone(tz).date()
    last = (window.end.astimezone(tz) - timedelta(microseconds=1)).date()
    return DateRange.model_validate({"from": first, "to": last})
