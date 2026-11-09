"""The contact window (Gate 3 decision 7): pure, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from errand.clock import IST
from errand.followup import quiet
from errand.store.repos.memories import MemoryRow

WINDOW = quiet.ContactWindow(time(9), time(19))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def row(value: object, *, status: str = "active", scope: str = "global") -> MemoryRow:
    return MemoryRow(
        "m-1", "u", "contact_window", scope, value, "h", "explicit", status,  # type: ignore[arg-type]
        None, None, None, 0, None, None, None,
    )  # fmt: skip


def test_the_window_is_open_from_its_start_up_to_but_not_including_its_end() -> None:
    assert not WINDOW.contains(at(9, 8, 59), IST)
    assert WINDOW.contains(at(9, 9), IST)
    assert WINDOW.contains(at(9, 18, 59), IST)
    assert not WINDOW.contains(at(9, 19), IST)


def test_the_next_opening_is_today_before_the_window_and_tomorrow_after_it() -> None:
    assert WINDOW.next_opening(at(9, 8), IST) == at(9, 9)
    assert WINDOW.next_opening(at(9, 19, 30), IST) == at(10, 9)
    assert WINDOW.next_opening(at(9, 23, 59), IST) == at(10, 9)
    assert WINDOW.next_opening(at(9, 12), IST) == at(9, 12)  # already open


def test_the_zone_decides_not_the_instant() -> None:
    nine_ist = datetime(2026, 11, 9, 3, 30, tzinfo=UTC)  # 09:00 in Asia/Kolkata
    assert WINDOW.contains(nine_ist, IST)
    assert not WINDOW.contains(nine_ist - timedelta(minutes=1), IST)


def test_a_saved_window_is_the_active_global_row_only() -> None:
    saved = quiet.saved([row({"from": "09:00", "to": "19:00"})])
    assert saved == WINDOW
    assert quiet.saved([]) is None
    assert quiet.saved([row({"from": "09:00", "to": "19:00"}, status="forgotten")]) is None
    assert quiet.saved([row({"from": "19:00", "to": "09:00"})]) is None  # refused by the registry
    assert quiet.saved([row("not a window")]) is None
