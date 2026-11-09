"""The booking failure paths that the scenarios do not reach on their own (M5).

Each test drives the real API and agent over the in-process world and reads the records.
The scenarios (S19 to S71) cover the main paths; these cover the budget at 0, the world's
refusal on the same-key retry, a lapse during verify, `hold_expired` as the answer to the
first send, the one-automatic-re-hold bound read from agent.sqlite, an abandoned errand,
and the wording of every new sentence against the claim guard and INV-04.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from errand.agent import templates
from errand.agent.claim_guard import guard
from errand.agent.nodes import verify as verify_node
from errand.clock import IST
from errand.config import BookingSettings, LimitSettings
from errand.domain.messages import ActionCard
from errand.domain.outcomes import PendingCheck
from errand.execution import executor
from errand.world.contracts import BookError
from errand.world.scenario import (
    BookConfig,
    FaultsConfig,
    QuoteDrift,
    ScenarioConfig,
    StatusOutage,
    WorldConfig,
)
from evals import invariants
from evals.runner import Harness, harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def world(
    *forced: str,
    drift: int = 0,
    dedupe: bool = True,
    faults_status_down: int = 0,
    **extra: Any,
) -> ScenarioConfig:
    quote = (
        QuoteDrift(mode="on_rehold", providers={f"P{n}": drift for n in range(1, 21)})
        if drift
        else QuoteDrift()
    )
    faults = (
        FaultsConfig(get_booking_status=StatusOutage(unavailable_n=faults_status_down))
        if faults_status_down
        else FaultsConfig()
    )
    return ScenarioConfig(
        id="m5_paths",
        seed=5,
        faults=faults,
        world=WorldConfig(
            book=BookConfig(forced=list(forced)),  # type: ignore[arg-type]
            enforce_idempotency=dedupe,
            quote_drift=quote,
            **extra,
        ),
    )


async def card_of(h: Harness) -> dict[str, Any]:
    convo = await h.conversation()
    cards = [m for m in convo["messages"] if m["body"].get("type") in ("action", "reminder")]
    assert cards, "no approval card"
    return cards[-1]


async def approve(h: Harness) -> Any:
    view = await h.say(OPENER)
    assert view.outcome == "AWAITING_APPROVAL", view.outcome
    card = await card_of(h)
    click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    return await h.finish(click.json()["run_id"])


def ops(rows: list[dict[str, Any]], op: str) -> int:
    return sum(1 for r in rows if r["op"] == op)


async def test_a_zero_retry_budget_is_the_pure_wait_rule(tmp_path: Path) -> None:
    """`booking.max_same_key_retries: 0`: the S23 world ends PENDING_VERIFICATION with one
    book, where the default budget retries and books."""
    booking = BookingSettings(max_same_key_retries=0)
    async with harness(tmp_path, world("timeout_not_booked"), booking=booking) as h:
        view = await approve(h)
        rec = await h.records()
    assert view.outcome == "PENDING_VERIFICATION"
    assert ops(rec.op_log, "book") == 1 and ops(rec.op_log, "get_booking_status") == 1
    assert [i["state"] for i in rec.intents] == ["needs_attention"]
    assert [i["same_key_retries"] for i in rec.intents] == [0]
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_the_environment_sets_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    assert BookingSettings().max_same_key_retries == 1
    assert BookingSettings().provider_dedupes_keys is True
    monkeypatch.setenv("ERRAND_BOOKING_MAX_SAME_KEY_RETRIES", "0")
    monkeypatch.setenv("ERRAND_BOOKING_PROVIDER_DEDUPES_KEYS", "false")
    settings = BookingSettings()
    assert settings.max_same_key_retries == 0 and settings.provider_dedupes_keys is False


async def test_a_dedupe_off_world_never_books_twice_on_a_hold_even_with_a_budget(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world("timeout_not_booked", dedupe=False)) as h:
        assert h.booking.provider_dedupes_keys is False and h.booking.max_same_key_retries == 1
        view = await approve(h)
        rec = await h.records()
    assert view.outcome == "PENDING_VERIFICATION" and ops(rec.op_log, "book") == 1


async def test_the_retry_gets_slot_taken_and_the_user_gets_other_options(tmp_path: Path) -> None:
    """The world answers the same-key retry with slot_taken: its answer for this key.
    The attempt is not_applied, the intent failed, and the planner offers alternatives."""
    async with harness(tmp_path, world("timeout_not_booked", "slot_taken")) as h:
        view = await approve(h)
        rec = await h.records()
    assert view.outcome == "PRESENTED_OPTIONS"
    assert (
        ops(rec.op_log, "book") == 2
        and len({r["idempotency_key"] for r in rec.op_log if r["op"] == "book"}) == 1
    )
    assert [(a["state"], a["calls"]) for a in rec.attempts] == [("not_applied", 2)]
    assert [(i["state"], i["same_key_retries"]) for i in rec.intents] == [("failed", 1)]
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_not_found_after_the_hold_lapsed_is_definitive_and_asks_for_a_fresh_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status check happens after the hold lapsed by the world clock: a late request
    can only get hold_expired, so `not_found` is definitive: nothing booked, a fresh
    quote, no retry."""
    monkeypatch.setattr(verify_node, "BACKOFF_S", (700.0, 1.0, 2.0, 4.0))
    # The 700 s wait is a test device to get past the hold's TTL in one run; INV-09 would
    # rightly flag it against the default 20 s grace, so the grace is raised to cover it.
    limits = LimitSettings(critical_grace_s=800.0, run_backstop_s=900.0)
    async with harness(tmp_path, world("timeout_not_booked"), limits=limits) as h:
        view = await approve(h)
        rec = await h.records()
        convo = await h.conversation()
    assert view.outcome == "ACTION_FAILED"
    assert ops(rec.op_log, "book") == 1
    assert [(a["state"], i["state"]) for a, i in zip(rec.attempts, rec.intents, strict=True)] == [
        ("not_applied", "failed")
    ]
    last = [m for m in convo["messages"] if m["role"] == "assistant"][-1]["body"]
    assert "nothing was booked" in last["text"]
    assert [a["kind"] for a in last["actions"]] == ["refresh_hold"]
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


class _RefuseFirstSend:
    """Makes the first `book` the answer `hold_expired` without reaching the world, as if
    the hold lapsed between the preflight and the provider (the preflight margin makes
    that unreachable through the clock alone)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent = 0
        real = executor.ConsequentialClient.book

        async def book(self_: Any, token: Any, **kw: Any) -> Any:
            self.sent += 1
            if self.sent == 1:
                return BookError(error="hold_expired")
            return await real(self_, token, **kw)

        monkeypatch.setattr(executor.ConsequentialClient, "book", book)


async def test_hold_expired_on_the_first_send_re_holds_and_sends_attempt_2_on_a_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _RefuseFirstSend(monkeypatch)
    async with harness(tmp_path, world("success")) as h:
        view = await approve(h)
        rec = await h.records()
    assert view.outcome == "BOOKED_VERIFIED"
    assert ops(rec.op_log, "hold_slot") == 2 and ops(rec.op_log, "book") == 1
    assert [(a["n"], a["state"]) for a in rec.attempts] == [(1, "not_applied"), (2, "applied")]
    assert rec.attempts[0]["idempotency_key"] != rec.attempts[1]["idempotency_key"]
    assert rec.attempts[0]["hold_id"] != rec.attempts[1]["hold_id"]
    assert [(i["state"], i["same_key_retries"]) for i in rec.intents] == [("succeeded", 0)]
    assert [r["type"] for r in rec.all_records()].count("approval.hold_rebound") == 1
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_hold_expired_on_the_first_send_with_a_changed_quote_asks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval is already consumed, so the intent fails and the user gets a new card
    on the hold that was just placed; nothing is booked and there is no attempt 2."""
    _RefuseFirstSend(monkeypatch)
    async with harness(tmp_path, world("success", drift=250)) as h:
        view = await approve(h)
        rec = await h.records()
        convo = await h.conversation()
    assert view.outcome == "AWAITING_APPROVAL"
    assert ops(rec.op_log, "book") == 0 and len(rec.attempts) == 1
    assert [a["state"] for a in rec.attempts] == ["not_applied"]
    assert [i["state"] for i in rec.intents] == ["failed"]
    assert [a["status"] for a in rec.approvals] == ["consumed", "pending"]
    texts = " ".join(str(m["body"].get("text", "")) for m in convo["messages"])
    assert "the price changed" in texts and "nothing was booked" in texts
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_hold_expired_after_a_re_hold_is_not_held_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval's one automatic re-hold is spent (read from agent.sqlite), so
    `hold_expired` on the first send fails the intent and asks for a fresh card."""
    _RefuseFirstSend(monkeypatch)
    async with harness(tmp_path, world("success")) as h:
        card = await _card_after_a_lapse(h)
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        view = await h.finish(click.json()["run_id"])
        rec = await h.records()
    assert view.outcome == "ACTION_FAILED"
    assert ops(rec.op_log, "hold_slot") == 2 and ops(rec.op_log, "book") == 0
    assert [(i["state"]) for i in rec.intents] == ["failed"]
    assert [a["status"] for a in rec.approvals] == ["consumed"]


async def _card_after_a_lapse(h: Harness) -> dict[str, Any]:
    view = await h.say(OPENER)
    assert view.outcome == "AWAITING_APPROVAL"
    card = await card_of(h)
    await h.inspector.advance(11 * 60)  # the hold lapses; the card (30 minutes) does not
    return card


async def test_the_one_re_hold_per_approval_is_read_from_the_database(tmp_path: Path) -> None:
    """`rehold_used` compares the hold the approval is bound to with the one its card was
    issued on, both in agent.sqlite; a bound hold with another expiry means it was moved."""
    from errand.agent.nodes.booking import BookingPath

    async with harness(tmp_path, world("success")) as h:
        view = await h.say(OPENER)
        assert view.outcome == "AWAITING_APPROVAL"
        card = await card_of(h)
        async with h.manager.db.session() as repos:
            row = await repos.approvals.get(card["approval_id"])
        assert row is not None
        path = BookingPath(deps=_DepsWithDb(h.manager.db), errand=None)  # type: ignore[arg-type]
        assert (
            ActionCard.model_validate_json(row.card_json).hold_id == row.bound_hold_id is not None
        )
        assert await path.rehold_used(row) is False
        async with h.manager.db.transaction() as repos:
            old = await repos.holds.get_by_hold_id(row.bound_hold_id or "")
            assert old is not None
            await repos.holds.insert(
                type(old)(
                    **{
                        **old.__dict__,
                        "hold_record_id": old.hold_record_id + "x",
                        "generation": old.generation + 1,
                        "hold_id": "H-moved",
                        "expires_at": old.expires_at + 600_000,
                    }
                )
            )
            await repos.conn.execute(
                "UPDATE approvals SET bound_hold_id = 'H-moved' WHERE approval_id = ?",
                (row.approval_id,),
            )
            moved = await repos.approvals.get(row.approval_id)
        assert moved is not None and await path.rehold_used(moved) is True
        # Identity, not expiry: the same expiry on the replacement changes nothing.
        async with h.manager.db.transaction() as repos:
            await repos.conn.execute(
                "UPDATE holds SET expires_at = ? WHERE hold_id = 'H-moved'", (old.expires_at,)
            )
            same_expiry = await repos.approvals.get(row.approval_id)
        assert same_expiry is not None and await path.rehold_used(same_expiry) is True
        # A card without the field (a pre-M5 row) counts as already re-held: fail safe.
        legacy = json.loads(row.card_json)
        del legacy["hold_id"]
        async with h.manager.db.transaction() as repos:
            await repos.conn.execute(
                "UPDATE approvals SET card_json = ?, bound_hold_id = ? WHERE approval_id = ?",
                (json.dumps(legacy), row.bound_hold_id, row.approval_id),
            )
            old_card = await repos.approvals.get(row.approval_id)
        assert old_card is not None and await path.rehold_used(old_card) is True


async def test_a_clean_confirmed_followed_by_not_found_is_a_contradiction_not_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9.4 step 6 allows the same-key retry only after an ambiguous call. `book` answered
    `confirmed`; the status then says `not_found`: nothing is sent again, nothing is
    reported failed, and the user is told the check is unfinished."""
    from errand.tools import gateway
    from errand.world.contracts import BookingStatusOut

    real = gateway.ToolGateway.call

    async def call(self: Any, caller: Any, tool: Any, args: Any, **kw: Any) -> Any:
        if caller is gateway.Caller.VERIFIER and tool == "get_booking_status":
            return BookingStatusOut(status="not_found")
        return await real(self, caller, tool, args, **kw)

    monkeypatch.setattr(gateway.ToolGateway, "call", call)
    async with harness(tmp_path, world("success")) as h:
        view = await approve(h)
        rec = await h.records()
        notice = await _last_notice(h)
    assert view.outcome == "PENDING_VERIFICATION"
    assert ops(rec.op_log, "book") == 1
    assert [i["state"] for i in rec.intents] == ["needs_attention"]
    assert [i["same_key_retries"] for i in rec.intents] == [0]
    assert notice["pending"]["reason"] == "check_incomplete"
    assert "couldn't finish checking" in notice["text"]
    checks = [r for r in rec.all_records() if r["type"] == "status.check"]
    assert checks and all(not r.get("mismatch") for r in checks)


class _DepsWithDb:
    def __init__(self, db: Any) -> None:
        self.db = db


async def test_an_abandoned_errand_gets_no_retry() -> None:
    """The retry condition reads "errand not ABANDONED" from the record now, so M6 only
    has to add the action (decision 13)."""
    from types import SimpleNamespace

    from errand.domain.errand import Phase

    check = verify_node._Verify.__new__(verify_node._Verify)
    check.deps = SimpleNamespace(booking_settings=BookingSettings())  # type: ignore[assignment]
    intent: Any = SimpleNamespace(same_key_retries=0)
    check.called = SimpleNamespace(answer="unknown")  # type: ignore[assignment]
    check.abandoned = False
    assert check._may_retry(intent) is True
    check.abandoned = True
    assert check._may_retry(intent) is False
    # The design allows the retry only after an ambiguous answer, never after a clean one.
    check.abandoned = False
    check.called = SimpleNamespace(answer="confirmed")  # type: ignore[assignment]
    assert check._may_retry(intent) is False
    assert Phase.ABANDONED.value == "abandoned"


# ---------------------------------------------------------------- the new sentences


def _claims(text: str) -> list[str]:
    found: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        found += [
            sentence
            for m in invariants._CLAIM.finditer(sentence)
            if not invariants._exempt(sentence, m, frozenset())
        ]
    return found


_TEXTS = [
    templates.pending_text(
        PendingCheck(approval_id="a", intent_id="i", reason="check_incomplete"), IST
    ),
    templates.pending_text(
        PendingCheck(
            approval_id="a",
            intent_id="i",
            reason="hold_live",
            lapse_at="2026-11-09T09:12:00+05:30",
        ),
        IST,
    ),
    templates.pending_text(
        PendingCheck(approval_id="a", intent_id="i", reason="status_unavailable"), IST
    ),
    templates.pending_text(
        PendingCheck(
            approval_id="a",
            intent_id="i",
            reason="material_mismatch",
            detail="a price of ₹1,500 instead of the ₹1,350 you approved",
            booking_id="B-5",
        ),
        IST,
    ),
    templates.NOT_BOOKED_TEXT,
    templates.SLOT_TAKEN_TEXT,
    templates.requote_text("price_changed", "₹1,350 is now ₹1,600") or "",
    templates.requote_text("slot_changed", "Thu 13 Nov 09:00 is now Thu 13 Nov 10:00") or "",
    templates.requote_text("hold_lapsed", None) or "",
    templates.requote_text("material_changed", None) or "",
    templates.provider_cancelled_text("Ravi Plumbing", "Thu 13 Nov 09:00"),
]


def _verify_with(
    book: dict[str, Any], listed: Any
) -> tuple[verify_node._Verify, list[tuple[str, str]]]:
    """A _Verify over a fake gateway whose list_bookings answers `listed` (or raises it)."""
    from types import SimpleNamespace

    from errand.domain.outcomes import BookCalled

    asked: list[tuple[str, str]] = []

    class Tools:
        async def call(self, caller: Any, tool: str, args: Any, **kw: Any) -> Any:
            asked.append((caller.value, tool))
            if isinstance(listed, Exception):
                raise listed
            return listed

    check = verify_node._Verify.__new__(verify_node._Verify)
    check.deps = SimpleNamespace(tools=Tools(), user_id="u_1", tz=IST)  # type: ignore[assignment]
    check.called = BookCalled(approval_id="a", intent_id="i", attempt_id="t", **book)
    return check, asked


def _material() -> Any:
    from errand.domain.material import BookMaterial
    from errand.world.contracts import Service

    return BookMaterial(
        user_id="u_1",
        provider_id="P1",
        service=Service.PLUMBER,
        slot_start="2026-11-13T09:00:00+05:30",
        slot_end="2026-11-13T10:00:00+05:30",
        quoted_price_inr=1350,
    )


def _slot(hour: int) -> Any:
    from datetime import datetime

    from errand.world.contracts import Slot

    return Slot(
        start=datetime.fromisoformat(f"2026-11-13T{hour:02d}:00:00+05:30"),
        end=datetime.fromisoformat(f"2026-11-13T{hour + 1:02d}:00:00+05:30"),
    )


def _listed(provider_id: str, provider_name: str, booking_id: str = "B-2") -> Any:
    from errand.world.contracts import BookingItem, ListBookingsOut, Service

    return ListBookingsOut(
        bookings=[
            BookingItem(
                booking_id=booking_id,
                provider_id=provider_id,
                provider_name=provider_name,
                service=Service.PLUMBER,
                slot=_slot(9),
                status="confirmed",
            )
        ]
    )


_CONFIRMED = {"answer": "confirmed", "booking_id": "B-2", "price_inr": 1350}
_APPROVED = verify_node.Approved("Ravi Plumbing", "Fri 13 Nov, 09:00-10:00")
_OK = _listed("P1", "Ravi Plumbing")


def _comparison_cases() -> list[Any]:
    from errand.world.contracts import BookingStatusOut

    ok = BookingStatusOut(status="confirmed", booking_id="B-2", slot=_slot(9))
    return [
        pytest.param(_CONFIRMED, ok, _OK, "b-id-ok", id="matches"),
        pytest.param(
            {**_CONFIRMED, "booking_id": "B-1"}, ok, _OK, "a booking number", id="booking_id"
        ),
        pytest.param(
            _CONFIRMED,
            BookingStatusOut(status="confirmed", booking_id="B-2", slot=_slot(10)),
            _OK,
            "10:00-11:00",
            id="slot",
        ),
        pytest.param(
            _CONFIRMED,
            BookingStatusOut(status="confirmed", booking_id="B-2"),
            _OK,
            "a different slot instead of the",
            id="slot_missing",
        ),
        pytest.param({**_CONFIRMED, "price_inr": 1600}, ok, _OK, "₹1,600", id="price"),
        pytest.param(
            _CONFIRMED, ok, _listed("P9", "Sharma Plumbing"), "Sharma Plumbing", id="provider"
        ),
        pytest.param(
            _CONFIRMED, ok, _listed("P1", "Ravi Plumbing", "B-7"), "not in your list", id="unlisted"
        ),
    ]


@pytest.mark.parametrize(("book", "status", "listed", "expect"), _comparison_cases())
async def test_every_difference_names_what_the_provider_recorded(
    book: dict[str, Any], status: Any, listed: Any, expect: str
) -> None:
    check, _ = _verify_with(book, listed)
    found = await check._compare(_material(), status, _APPROVED)
    if expect == "b-id-ok":
        assert found.differences == [] and found.incomplete is False
        return
    assert found.incomplete is False and len(found.differences) == 1
    text = templates.pending_text(
        PendingCheck(
            approval_id="a",
            intent_id="i",
            reason="material_mismatch",
            detail="; ".join(found.differences),
            booking_id="B-2",
        ),
        IST,
    )
    assert expect in text, text
    for internal in ("book answered", "could not be checked", "recorded the provider"):
        assert internal not in text
    assert guard(text, [], IST).removed == [] and _claims(text) == [], text
    if "slot" in found.differences[0]:
        assert "you approved" in text or "instead of" in text
    if expect == "Sharma Plumbing":
        assert "instead of Ravi Plumbing" in text


async def test_a_list_failure_is_an_incomplete_check_with_its_own_sentence() -> None:
    from errand.tools.errors import ToolTransient
    from errand.world.contracts import BookingStatusOut

    ok = BookingStatusOut(status="confirmed", booking_id="B-2", slot=_slot(9))
    check, asked = _verify_with(_CONFIRMED, ToolTransient("list_bookings", 503, b""))
    found = await check._compare(_material(), ok, _APPROVED)
    assert found.differences == [] and found.incomplete is True
    assert asked == [("verifier", "list_bookings")]
    text = templates.pending_text(
        PendingCheck(
            approval_id="a",
            intent_id="i",
            reason="check_incomplete",
            booking_id="B-2",
        ),
        IST,
    )
    assert "couldn't finish checking" in text and "recorded" not in text
    assert guard(text, [], IST).removed == [] and _claims(text) == [], text


@pytest.mark.parametrize(
    "detail",
    ["the booking as no longer active", "a confirmation without a booking number"],
)
def test_the_odd_status_answers_read_as_what_the_provider_recorded(detail: str) -> None:
    text = templates.pending_text(
        PendingCheck(approval_id="a", intent_id="i", reason="material_mismatch", detail=detail),
        IST,
    )
    assert text.startswith(f"The provider recorded {detail},")
    assert guard(text, [], IST).removed == [] and _claims(text) == [], text


def test_the_phrases_verify_writes_for_an_odd_status_are_the_ones_tested_above() -> None:
    from errand.world.contracts import BookingStatusOut

    assert (
        verify_node._status_phrase(BookingStatusOut(status="cancelled"))
        == "the booking as no longer active"
    )
    assert (
        verify_node._status_phrase(BookingStatusOut(status="confirmed"))
        == "a confirmation without a booking number"
    )


@pytest.mark.parametrize("text", _TEXTS)
def test_every_new_sentence_survives_the_claim_guard_and_inv04(text: str) -> None:
    assert text
    assert guard(text, [], IST).removed == []
    assert _claims(text) == []


@pytest.mark.parametrize("lapse", ["2026-11-09T09:12:00+05:30", "2026-11-09T03:42:00+00:00"])
def test_the_wait_rule_names_the_lapse_time_in_ist(lapse: str) -> None:
    text = templates.pending_text(
        PendingCheck(approval_id="a", intent_id="i", reason="hold_live", lapse_at=lapse), IST
    )
    assert "I'll know for sure by 09:12" in text


def test_status_unavailable_promises_no_time() -> None:
    text = templates.pending_text(
        PendingCheck(approval_id="a", intent_id="i", reason="status_unavailable"), IST
    )
    assert "know for sure by" not in text and "update you here" in text


async def test_every_book_goes_out_after_its_write_ahead_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4 and step 7: the attempt is `in_flight` with its call counted, and for the
    retry `same_key_retries` is bumped, in a transaction committed before `book` is sent.
    Read from a second connection at the moment of the call, so only committed rows show."""
    import sqlite3

    seen: list[tuple[int, int, str, int]] = []
    real = executor.ConsequentialClient.book

    async def book(self_: Any, token: Any, **kw: Any) -> Any:
        conn = sqlite3.connect(f"file:{tmp_path / 'agent.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT a.calls, i.same_key_retries, a.state, a.status_checks "
                "FROM intent_attempts a JOIN action_intents i USING (intent_id)"
            ).fetchone()
        finally:
            conn.close()
        seen.append(tuple(row))
        return await real(self_, token, **kw)

    monkeypatch.setattr(executor.ConsequentialClient, "book", book)
    async with harness(tmp_path, world("timeout_not_booked")) as h:
        view = await approve(h)
    assert view.outcome == "BOOKED_VERIFIED"
    # first send: calls 1, no retry yet; the retry: calls 2, one retry, the first pass counted
    assert seen == [(1, 0, "in_flight", 0), (2, 1, "in_flight", 1)]


async def _last_notice(h: Harness) -> dict[str, Any]:
    convo = await h.conversation()
    return [m for m in convo["messages"] if m["role"] == "assistant"][-1]["body"]


async def test_the_pending_notice_carries_its_facts_for_the_card(tmp_path: Path) -> None:
    """`OutcomeNotice.pending` (additive, domain/messages.py): the shape M7's VerifyingCard
    is built on. Facts only; there is no action to render until M6."""
    async with harness(tmp_path / "wait", world("timeout_not_booked", "timeout_not_booked")) as h:
        await approve(h)
        wait = await _last_notice(h)
    assert wait["outcome"] == "PENDING_VERIFICATION" and wait["actions"] == []
    assert wait["pending"]["reason"] == "hold_live"
    assert wait["pending"]["lapse_label"] == "08:10" and wait["pending"]["lapse_at"].endswith(
        "+05:30"
    )
    assert wait["pending"]["booking_id"] is None and "I'll know for sure by 08:10" in wait["text"]

    down = world("timeout_booked", faults_status_down=4)
    async with harness(tmp_path / "down", down) as h:
        await approve(h)
        unavailable = await _last_notice(h)
    assert unavailable["pending"]["reason"] == "status_unavailable"
    assert (
        unavailable["pending"]["lapse_at"] is None and unavailable["pending"]["lapse_label"] is None
    )
