"""timewin.resolve: the closed set of phrases and the rulings behind them (10.5).

One of the few places a unit test is the right proof: date arithmetic is pure, the
inputs are a clock reading and a phrase, and a wrong Thursday is a booking error.
Scenarios start Monday 2026-11-09 08:00 IST.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from errand.agent.timewin import SUPPORTED_PHRASES, Unresolved, date_range, resolve
from errand.clock import IST
from errand.domain.brief import TimeWindow, TimeWindowExpr

MON = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


def at(day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def win(phrase: str, now: datetime = MON, **kw: object) -> TimeWindow:
    out = resolve(TimeWindowExpr(phrase=phrase, **kw), now, IST)  # type: ignore[arg-type]
    assert isinstance(out, TimeWindow), out
    return out


def unresolved(phrase: str, now: datetime = MON, **kw: object) -> Unresolved:
    out = resolve(TimeWindowExpr(phrase=phrase, **kw), now, IST)  # type: ignore[arg-type]
    assert isinstance(out, Unresolved), out
    return out


def days(w: TimeWindow) -> tuple[date, date]:
    r = date_range(w, IST)
    return r.from_, r.to


def test_today_and_tomorrow() -> None:
    assert days(win("today")) == (date(2026, 11, 9), date(2026, 11, 9))
    assert days(win("tomorrow")) == (date(2026, 11, 10), date(2026, 11, 10))
    assert win("today").start == MON  # never before now


def test_this_week_runs_from_now_to_sunday() -> None:
    w = win("this week")
    assert days(w) == (date(2026, 11, 9), date(2026, 11, 15))
    assert w.label == "Mon 9 Nov - Sun 15 Nov"
    assert w.weekdays is None


def test_next_week_is_monday_to_sunday() -> None:
    w = win("next week")
    assert days(w) == (date(2026, 11, 16), date(2026, 11, 22))
    assert w.start == at(16, 0)


@pytest.mark.parametrize(
    ("name", "short", "day"),
    [
        ("monday", "mon", 9),
        ("tuesday", "tue", 10),
        ("wednesday", "wed", 11),
        ("thursday", "thu", 12),
        ("friday", "fri", 13),
        ("saturday", "sat", 14),
        ("sunday", "sun", 15),
    ],
)
def test_every_weekday_name_is_its_next_occurrence(name: str, short: str, day: int) -> None:
    for spelling in (name, short, name.upper(), f"  On {name}. "):
        w = win(spelling)
        assert days(w) == (date(2026, 11, day), date(2026, 11, day)), spelling


def test_a_bare_weekday_today_counts_only_while_the_day_is_still_open() -> None:
    assert days(win("monday")) == (date(2026, 11, 9), date(2026, 11, 9))
    # 14:00 on Monday, "monday morning" is over: the next Monday
    late = at(9, 14)
    assert days(win("monday", late, part_of_day="morning")) == (
        date(2026, 11, 16),
        date(2026, 11, 16),
    )
    # ... but "monday afternoon" is still ahead
    assert days(win("monday", late, part_of_day="afternoon")) == (
        date(2026, 11, 9),
        date(2026, 11, 9),
    )
    # and with no daily window, any time today is open
    assert days(win("monday", at(9, 23, 30))) == (date(2026, 11, 9), date(2026, 11, 9))


def test_this_weekday_is_the_day_in_this_monday_sunday_week() -> None:
    assert days(win("this thursday")) == (date(2026, 11, 12), date(2026, 11, 12))
    assert days(win("this mon")) == (date(2026, 11, 9), date(2026, 11, 9))
    wed = at(11)
    assert days(win("this friday", wed)) == (date(2026, 11, 13), date(2026, 11, 13))


def test_this_weekday_already_passed_is_unresolved_not_guessed() -> None:
    out = unresolved("this monday", at(11))
    assert "already passed" in out.reason
    # today, but the daily window is over
    assert "already passed" in unresolved("this monday", at(9, 14), part_of_day="morning").reason


def test_next_weekday_is_that_day_in_next_calendar_week() -> None:
    # not "the coming Thursday": on Monday 9th the coming Thursday is the 12th
    assert days(win("next thursday")) == (date(2026, 11, 19), date(2026, 11, 19))
    assert days(win("next monday")) == (date(2026, 11, 16), date(2026, 11, 16))
    # consistent with "next week" even late in the week
    sat = at(14)
    assert days(win("next friday", sat)) == (date(2026, 11, 20), date(2026, 11, 20))
    assert days(win("next week", sat))[0] == date(2026, 11, 16)


def test_this_weekend_on_a_weekday_a_saturday_and_a_sunday() -> None:
    w = win("this weekend")
    assert days(w) == (date(2026, 11, 14), date(2026, 11, 15))
    assert w.label == "Sat 14 Nov - Sun 15 Nov"
    assert days(win("this weekend", at(14, 9))) == (date(2026, 11, 14), date(2026, 11, 15))
    assert days(win("this weekend", at(15, 9))) == (date(2026, 11, 15), date(2026, 11, 15))
    assert "already passed" in unresolved("this weekend", at(15, 22), part_of_day="morning").reason


def test_next_weekend_is_next_weeks_saturday_and_sunday() -> None:
    assert days(win("next weekend")) == (date(2026, 11, 21), date(2026, 11, 22))
    assert days(win("next weekend", at(15, 9))) == (date(2026, 11, 21), date(2026, 11, 22))


def test_a_weekday_list_sets_the_weekdays_and_spans_first_to_last() -> None:
    w = win("Thursday or Friday", part_of_day="any", not_before=time(10), not_after=time(12))
    assert w.weekdays == frozenset({4, 5})
    assert days(w) == (date(2026, 11, 12), date(2026, 11, 13))
    assert w.label == "Thu 12 Nov - Fri 13 Nov, 10:00-12:00"
    assert (w.daily_from, w.daily_to) == (time(10), time(12))
    for spelling in ("thu and fri", "Thursday, Friday", "thu, fri or sat"):
        assert win(spelling).weekdays is not None


def test_a_non_contiguous_list_names_its_days() -> None:
    w = win("Thursday or Monday", not_before=time(10))
    assert w.weekdays == frozenset({1, 4})
    # Monday is today at 08:00 and the window is ahead; Thursday is the 12th
    assert days(w) == (date(2026, 11, 9), date(2026, 11, 12))
    assert w.label == "Mon 9 Nov or Thu 12 Nov, 10:00-23:59"
    thu = at(12, 9)
    w2 = win("thursday or monday", thu)
    assert days(w2) == (date(2026, 11, 12), date(2026, 11, 16))
    assert w2.label == "Thu 12 Nov or Mon 16 Nov"


def test_a_list_window_only_admits_the_listed_days() -> None:
    from errand.world.contracts import Slot

    w = win("Thursday or Friday")
    thu = Slot(start=at(12, 9), end=at(12, 10))
    sat = Slot(start=at(14, 9), end=at(14, 10))
    mon = Slot(start=at(16, 9), end=at(16, 10))
    from errand.agent.timewin import slot_fits

    assert slot_fits(thu, w, IST)
    assert not slot_fits(sat, w, IST)
    assert not slot_fits(mon, w, IST)


def test_mornings_afternoons_evenings_come_from_config() -> None:
    assert (
        win("today", part_of_day="morning").daily_from,
        win("today", part_of_day="morning").daily_to,
    ) == (
        time(7),
        time(12),
    )
    a = win("today", part_of_day="afternoon")
    e = win("today", part_of_day="evening")
    assert (a.daily_from, a.daily_to) == (time(12), time(17))
    assert (e.daily_from, e.daily_to) == (time(17), time(21))
    assert win("today", part_of_day="any").daily_from is None


def test_not_before_and_not_after_intersect_the_part_of_day() -> None:
    w = win("tomorrow", part_of_day="morning", not_before=time(10))
    assert (w.daily_from, w.daily_to) == (time(10), time(12))
    assert w.label == "Tue 10 Nov, 10:00-12:00"
    w = win("tomorrow", part_of_day="morning", not_after=time(9))
    assert (w.daily_from, w.daily_to) == (time(7), time(9))
    w = win("tomorrow", not_before=time(10))  # "not before 10 a.m."
    assert (w.daily_from, w.daily_to) == (time(10), None)


def test_an_empty_daily_window_is_unresolved() -> None:
    out = unresolved("tomorrow", part_of_day="morning", not_before=time(13))
    assert out.reason == "the daily window is empty"
    assert unresolved("tomorrow", not_before=time(15), not_after=time(14)).reason == (
        "the daily window is empty"
    )


@pytest.mark.parametrize(
    "phrase",
    ["the 14th", "14th", "14 nov", "next month", "in two days", "someday", "this", "next", ""],
)
def test_anything_else_is_unresolved_with_the_supported_list(phrase: str) -> None:
    out = unresolved(phrase or " ")
    assert "supported:" in out.reason
    for item in SUPPORTED_PHRASES:
        assert item in out.reason


def test_a_list_with_a_non_weekday_is_unresolved() -> None:
    assert isinstance(unresolved("thursday or the 14th"), Unresolved)
    assert isinstance(unresolved("this thursday or friday"), Unresolved)


def test_week_boundaries() -> None:
    sunday = at(15, 9)
    assert days(win("this week", sunday)) == (date(2026, 11, 15), date(2026, 11, 15))
    assert days(win("next week", sunday)) == (date(2026, 11, 16), date(2026, 11, 22))
    assert days(win("monday", sunday)) == (date(2026, 11, 16), date(2026, 11, 16))
    assert days(win("this monday", at(9, 23))) == (date(2026, 11, 9), date(2026, 11, 9))
    # Monday 00:00 is the first instant of a week
    assert days(win("this week", at(16, 0))) == (date(2026, 11, 16), date(2026, 11, 22))
    # Sunday late evening is still Sunday: next week starts tomorrow
    assert days(win("next week", at(15, 23, 59))) == (date(2026, 11, 16), date(2026, 11, 22))


def test_the_ist_offset_decides_which_day_it_is() -> None:
    # 19:00Z on Monday is 00:30 IST on Tuesday: "today" is Tuesday
    instant = datetime(2026, 11, 9, 19, 0, tzinfo=UTC)
    assert days(win("today", instant)) == (date(2026, 11, 10), date(2026, 11, 10))
    assert days(win("this week", instant)) == (date(2026, 11, 10), date(2026, 11, 15))
    assert days(win("tuesday", instant)) == (date(2026, 11, 10), date(2026, 11, 10))
    assert days(win("monday", instant)) == (date(2026, 11, 16), date(2026, 11, 16))
    assert win("today", instant).start == instant.astimezone(IST)


def test_a_window_never_starts_before_now_and_ends_at_midnight() -> None:
    w = win("thursday")
    assert w.start == at(12, 0)
    assert w.end == at(13, 0)
    assert days(win("today", at(9, 22))) == (date(2026, 11, 9), date(2026, 11, 9))
