"""INV-03 and INV-18 hold on real records and fail on a doctored copy (M5).

Every clause of INV-03 gets the smallest edit that models the bug it exists to catch:
a second key on one hold, a re-send without a `not_found`, a re-send after the hold
lapsed, an outcome shown before the status check, a third book, a second book with dedupe
off, a re-send straight after a timeout, two world bookings for one key, a book after
"Never mind". INV-18 and the INV-01 exception for a mismatch the agent caught likewise.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from evals import invariants
from evals.invariants import Records
from tests.evals.test_invariants import fails, records_of


@pytest.fixture
async def s23(tmp_path: Path) -> Records:
    return await records_of("S23_timeout_not_booked_same_key_retry", tmp_path / "s23")


@pytest.fixture
async def s22(tmp_path: Path) -> Records:
    return await records_of("S22_timeout_after_success", tmp_path / "s22")


@pytest.fixture
async def s69(tmp_path: Path) -> Records:
    return await records_of("S69_timeout_not_booked_dedupe_off", tmp_path / "s69")


@pytest.fixture
async def s68(tmp_path: Path) -> Records:
    return await records_of("S68_timeout_not_booked_twice", tmp_path / "s68")


@pytest.fixture
async def s24(tmp_path: Path) -> Records:
    return await records_of("S24_status_unavailable_after_timeout", tmp_path / "s24")


# The same three conversations stopped before the reconciler's tick: an attempt still
# waits on the provider, which is what INV-18's clauses are about.
@pytest.fixture
async def s69p(tmp_path: Path) -> Records:
    return await records_of(
        "S69_timeout_not_booked_dedupe_off", tmp_path / "s69p", before_tick=True
    )


@pytest.fixture
async def s68p(tmp_path: Path) -> Records:
    return await records_of("S68_timeout_not_booked_twice", tmp_path / "s68p", before_tick=True)


@pytest.fixture
async def s24p(tmp_path: Path) -> Records:
    return await records_of(
        "S24_status_unavailable_after_timeout", tmp_path / "s24p", before_tick=True
    )


@pytest.fixture
async def s65(tmp_path: Path) -> Records:
    return await records_of("S65_world_books_a_different_price", tmp_path / "s65")


def _run_of(rec: Records, tool: str) -> list[dict[str, Any]]:
    """The records of the one run that called `tool`."""
    for _, records in rec.run_records():
        if any(r["type"] == "tool.call" and r.get("tool") == tool for r in records):
            return records
    raise AssertionError(f"no run called {tool}")


def _calls(records: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [r for r in records if r["type"] == "tool.call" and r.get("tool") == tool]


def _insert_after(records: list[dict[str, Any]], anchor: dict[str, Any], *new: Any) -> None:
    at = records.index(anchor) + 1
    records[at:at] = [copy.deepcopy(r) for r in new]
    for i, r in enumerate(records, start=1):  # keep seq honest
        r["seq"] = i


def test_inv03_and_inv18_hold_and_count_what_they_checked(
    s22: Records,
    s23: Records,
    s24: Records,
    s68: Records,
    s69: Records,
    s24p: Records,
    s69p: Records,
) -> None:
    for rec in (s22, s23, s24, s68, s69, s24p, s69p):
        assert fails("INV-03", rec) == [] and fails("INV-18", rec) == []
    assert s23.notes["inv03_books_checked"] == 2 and s23.notes["inv03_resends_checked"] == 1
    assert s22.notes["inv03_resends_checked"] == 0
    assert s24p.notes["attempts_unsettled"] == 1 and s69p.notes["attempts_unsettled"] == 1
    # after the reconciler's tick nothing is left waiting
    assert s24.notes["attempts_unsettled"] == 0 and s69.notes["attempts_unsettled"] == 0
    assert s22.notes["attempts_unsettled"] == 0 and s22.notes["attempts_checked"] == 1


# ---------------------------------------------------------------- INV-03


def test_inv03_fails_on_a_second_key_on_one_hold(s23: Records) -> None:
    doctored = copy.deepcopy(s23)
    resend = _calls(_run_of(doctored, "book"), "book")[1]
    resend["args"]["idempotency_key"] = "bo_other"
    resend["idempotency_key"] = "bo_other"
    assert any("was booked under 2 keys" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_resend_with_no_not_found_before_it(s23: Records) -> None:
    doctored = copy.deepcopy(s23)
    records = _run_of(doctored, "book")
    first_status = _calls(records, "get_booking_status")[0]
    first_status["result"] = {"status": "confirmed", "booking_id": "B-1"}
    assert any("not a not_found" in v for v in fails("INV-03", doctored))
    records.remove(first_status)
    assert any("no status check, not a not_found" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_resend_after_the_hold_lapsed(s23: Records) -> None:
    doctored = copy.deepcopy(s23)
    for hold in doctored.world_holds.values():
        hold["expires_at"] = 1  # lapsed before every call the scenario made
    assert any("after the hold had lapsed" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_an_outcome_shown_before_the_status_check(s23: Records) -> None:
    doctored = copy.deepcopy(s23)
    records = _run_of(doctored, "book")
    records.remove(_calls(records, "get_booking_status")[1])
    assert any("an outcome was shown before a status check" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_third_book_over_the_budget(s23: Records) -> None:
    doctored = copy.deepcopy(s23)
    records = _run_of(doctored, "book")
    books, statuses = _calls(records, "book"), _calls(records, "get_booking_status")
    # not_found, book once more, status: a clean third call, but over 1 + max_same_key_retries
    not_found = {**copy.deepcopy(statuses[0]), "result": {"status": "not_found"}}
    _insert_after(records, statuses[1], not_found, books[1], statuses[1])
    assert doctored.booking is not None and doctored.booking["max_same_key_retries"] == 1
    assert any("3 book calls, at most 2 allowed" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_second_book_with_dedupe_off(s69: Records) -> None:
    doctored = copy.deepcopy(s69)
    records = _run_of(doctored, "book")
    (book,) = _calls(records, "book")
    (status,) = _calls(records, "get_booking_status")
    _insert_after(records, status, book, status)
    assert not doctored.world_dedupes
    assert any("with dedupe off, exactly 1 allowed" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_resend_straight_after_a_timeout(s22: Records) -> None:
    """The next op on the key after a timed-out book must be get_booking_status."""
    doctored = copy.deepcopy(s22)
    records = _run_of(doctored, "book")
    (book,) = _calls(records, "book")
    _insert_after(records, book, book)
    assert any(
        "after an ambiguous book the agent called book" in v for v in fails("INV-03", doctored)
    )


def test_inv03_reads_the_worlds_op_log_too(s22: Records) -> None:
    """The agent's own calls are untouched; the world saw a second book on the key where
    the status check was."""
    doctored = copy.deepcopy(s22)
    book = next(r for r in doctored.op_log if r["op"] == "book")
    doctored.op_log = [
        {**r, "op": "book"} if r["op"] == "get_booking_status" else r for r in doctored.op_log
    ]
    assert fails("INV-03", s22) == []
    assert any("the world's next op on the key" in v for v in fails("INV-03", doctored))
    assert book["op"] == "book"


def test_inv03_fails_on_two_world_bookings_for_one_key(s22: Records) -> None:
    doctored = copy.deepcopy(s22)
    made = next(b for b in doctored.world_bookings if b["created_by_key"])
    doctored.world_bookings.append({**made, "booking_id": "B-dup"})
    assert any("2 world bookings for one key" in v for v in fails("INV-03", doctored))


def test_inv03_fails_on_a_book_after_never_mind(s22: Records) -> None:
    doctored = copy.deepcopy(s22)
    records = _run_of(doctored, "book")
    (book,) = _calls(records, "book")
    abandoned = {
        **copy.deepcopy(records[0]),
        "type": "state.transition",
        "from": "executing",
        "to": "abandoned",
        "reason": "never_mind",
    }
    at = records.index(book)
    records.insert(at, abandoned)
    for i, r in enumerate(records, start=1):
        r["seq"] = i
    assert any("a book after the errand was abandoned" in v for v in fails("INV-03", doctored))


# ---------------------------------------------------------------- INV-18


def test_inv18_fails_when_the_intent_is_not_waiting_for_a_person(s24p: Records) -> None:
    doctored = copy.deepcopy(s24p)
    for intent in doctored.intents:
        intent["state"] = "failed"
    assert any("its intent is failed, not needs_attention" in v for v in fails("INV-18", doctored))


def test_inv18_fails_when_the_user_never_saw_pending_verification(s68p: Records) -> None:
    doctored = copy.deepcopy(s68p)
    for m in doctored.messages:
        if m["body"].get("outcome") == "PENDING_VERIFICATION":
            m["body"]["outcome"] = "REPLIED"
    assert any("never saw PENDING_VERIFICATION" in v for v in fails("INV-18", doctored))


def test_inv18_fails_on_an_in_flight_attempt_whose_run_ended_otherwise(s22: Records) -> None:
    doctored = copy.deepcopy(s22)
    doctored.attempts[0]["state"] = "in_flight"
    assert any("in_flight" in v for v in fails("INV-18", doctored))


# ---------------------------------------------------------------- INV-01 and a caught mismatch


def test_a_mismatch_the_agent_caught_is_not_an_inv01_violation_but_an_unnoticed_one_is(
    s65: Records,
) -> None:
    assert fails("INV-01", s65) == []
    doctored = copy.deepcopy(s65)
    for r in doctored.all_records():
        if r["type"] == "status.check":
            r["mismatch"] = None
    assert any("differs from the approved material" in v for v in fails("INV-01", doctored))
    leftover = copy.deepcopy(s65)
    for intent in leftover.intents:
        intent["state"] = "succeeded"
    assert any("differs from the approved material" in v for v in fails("INV-01", leftover))


def test_the_default_set_includes_the_m5_invariants() -> None:
    assert "INV-03" in invariants.DEFAULT and "INV-18" in invariants.DEFAULT
