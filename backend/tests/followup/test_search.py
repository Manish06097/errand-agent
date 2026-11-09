"""The replacement window and the price ceiling (Gate 3 decisions 9 and 10)."""

from __future__ import annotations

from datetime import datetime, timedelta

from errand.clock import IST
from errand.followup.search import Interval, price_ceiling, replacement_window, window_end

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
SLOT = datetime(2026, 11, 13, 9, 0, tzinfo=IST)  # Fri


def day(d: int, hour: int = 0) -> datetime:
    return datetime(2026, 11, d, hour, 0, tzinfo=IST)


def test_the_window_is_the_original_day_plus_or_minus_two() -> None:
    w = replacement_window(SLOT, NOW, IST)
    assert w == Interval(day(11), day(16))  # Wed 11 to the end of Sun 15


def test_the_window_starts_no_earlier_than_now_plus_two_hours() -> None:
    w = replacement_window(day(10, 9), NOW, IST)
    assert w is not None and w.start == NOW + timedelta(hours=2)
    assert w.end == day(13)


def test_urgent_is_today_and_tomorrow_only() -> None:
    soon = day(10, 9)
    w = replacement_window(soon, NOW, IST, urgent=True)
    assert w == Interval(NOW + timedelta(hours=2), day(11))


def test_a_reschedule_card_moves_the_window_to_the_day_it_was_moving_to() -> None:
    target = datetime(2026, 11, 20, 9, 0, tzinfo=IST)
    w = replacement_window(SLOT, NOW, IST, reschedule_target=target)
    assert w == Interval(day(18), day(23))


def test_nothing_is_left_when_the_window_is_past() -> None:
    assert replacement_window(day(9, 9), day(13, 12), IST) is None


def test_window_end_is_the_end_of_the_original_day_plus_two() -> None:
    assert window_end(SLOT, IST) == day(16)
    assert window_end(datetime(2026, 11, 13, 23, 30, tzinfo=IST), IST) == day(16)


def test_the_ceiling_is_the_first_that_exists() -> None:
    assert price_ceiling(1400, 1500, 1300) == 1400
    assert price_ceiling(None, 1500, 1300) == 1500
    assert price_ceiling(None, None, 1300) == 1300


def test_no_budget_falls_through_to_the_approved_price() -> None:
    assert price_ceiling(None, "none", 1300) == 1300
    assert price_ceiling(None, "none", None) is None
    assert price_ceiling(1400, "none", 1300) == 1400
