"""Pure helpers over resolved windows and slots, shared by the planner views and timewin."""

from __future__ import annotations

from datetime import date, tzinfo

from errand.domain.brief import TimeWindow
from errand.world.contracts import Slot

# Spelled out rather than strftime("%a"), which follows the process locale.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def slot_fits(slot: Slot, window: TimeWindow, tz: tzinfo) -> bool:
    start = slot.start.astimezone(tz)
    end = slot.end.astimezone(tz)
    if start < window.start or end > window.end:
        return False
    if window.weekdays is not None and start.isoweekday() not in window.weekdays:
        return False
    if window.daily_from is not None and start.time() < window.daily_from:
        return False
    if window.daily_to is None:
        return True
    return end.time() <= window.daily_to and end.date() == start.date()


def slot_label(slot: Slot, tz: tzinfo) -> str:
    start = slot.start.astimezone(tz)
    end = slot.end.astimezone(tz)
    return f"{fmt_day(start.date())}, {start:%H:%M}-{end:%H:%M}"
