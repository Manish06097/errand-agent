"""S5: the ledger of older history, built by code from booking records."""

from __future__ import annotations

from datetime import datetime

from errand.clock import IST, to_ms
from errand.context.ledger import S5_CAP, build_ledger, line_for
from errand.llm.tokens import est_tokens
from errand.store.repos.known_bookings import KnownBooking


def booking(n: int, status: str = "completed") -> KnownBooking:
    start = to_ms(datetime(2026, 9, 1 + n % 28, 11, 0, tzinfo=IST)) + n * 1000
    return KnownBooking(
        f"B{n:03d}",
        "u",
        "P4",
        "electrician",
        start,
        start + 3_600_000,
        status,
        start,
        None,
        None,
        None,
    )


def test_a_line_has_booking_record_fields_only() -> None:
    line = line_for(booking(1), IST, {"P4": "Arjun Electricals"})
    assert line == "2 Sep: electrician with Arjun Electricals, Wed 11:00, completed"
    assert line_for(booking(1), IST) == "2 Sep: electrician with P4, Wed 11:00, completed"


def test_no_history_is_no_block() -> None:
    assert build_ledger([], tz=IST).text == ""
    assert build_ledger([booking(1, "failed")], tz=IST).text == ""


def test_newest_first_and_the_rest_fold_into_one_overflow_line() -> None:
    rows = [booking(n) for n in range(1, 31)]
    ledger = build_ledger(rows, tz=IST)
    assert ledger.tokens <= S5_CAP and ledger.older > 0
    assert ledger.lines + ledger.older == 30
    lines = ledger.text.splitlines()
    assert lines[-2] == f"and {ledger.older} earlier errands"
    # the first shown line is the newest booking
    newest = max(rows, key=lambda b: b.slot_start)
    assert lines[1] == line_for(newest, IST)
    assert est_tokens(ledger.text) == ledger.tokens


def test_a_short_history_has_no_overflow_line() -> None:
    ledger = build_ledger([booking(1), booking(2)], tz=IST)
    assert ledger.older == 0 and ledger.lines == 2 and "earlier errands" not in ledger.text


def test_bookings_shown_elsewhere_can_be_left_out() -> None:
    ledger = build_ledger([booking(1), booking(2)], tz=IST, exclude=frozenset({"B002"}))
    assert ledger.lines == 1 and "B002" not in ledger.text
