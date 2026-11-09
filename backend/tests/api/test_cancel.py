"""Cancel (N1): the proposal checks, the approval path, and what a changed booking does.

The scenarios S47, S48 and S54b are the end-to-end proofs; these pin the pieces around
them: the booking handles, the pure validation, a typed grant, and a booking that is no
longer what the card showed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from errand.agent.changes.validate import check_change_proposal, convertible
from errand.agent.validation import Rejected
from errand.clock import IST
from errand.domain.booking_refs import BOOKING_REF, assign
from errand.domain.brief import Brief, ErrandKind
from errand.domain.errand import Errand, Phase
from errand.domain.slots import Shortlist
from errand.llm.scripted import ScriptedRequest
from errand.world.contracts import BookingItem, Service, Slot
from errand.world.scenario import ScenarioConfig
from evals import invariants
from evals.policies import reference
from evals.policies.common import answer, read, tool_calls
from evals.runner import Harness, harness

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


def item(booking_id: str, hours: int, status: str = "confirmed") -> BookingItem:
    start = NOW + timedelta(hours=hours)
    return BookingItem.model_validate(
        {
            "booking_id": booking_id,
            "provider_id": "A2",
            "provider_name": "Cool Tech",
            "service": Service.AC_SERVICING,
            "slot": Slot(start=start, end=start + timedelta(hours=1)),
            "status": status,
        }
    )


def errand(kind: ErrandKind = ErrandKind.BOOK, phase: Phase = Phase.GATHERING, **kw: Any) -> Errand:
    return Errand(errand_id="e_1", kind=kind, phase=phase, brief=Brief(), **kw)


# ------------------------------------------------------------------ handles


def test_handles_are_assigned_by_code_in_one_stable_order() -> None:
    later, sooner = item("B-9", 48), item("B-3", 24)
    refs = assign([later, sooner], limit=10)
    assert list(refs) == ["B1", "B2"] and refs["B1"] is sooner
    assert assign([sooner, later], limit=10) == refs  # input order never matters
    assert list(assign([later, sooner], limit=1)) == ["B1"]


@pytest.mark.parametrize("ref", ["B1", "B12", "B999"])
def test_a_handle_has_one_shape(ref: str) -> None:
    assert BOOKING_REF.fullmatch(ref)


@pytest.mark.parametrize("ref", ["B-1234", "b1", "B0", "B1234", "S1", "B1 ", ""])
def test_a_booking_id_or_a_malformed_handle_is_not_a_handle(ref: str) -> None:
    assert not BOOKING_REF.fullmatch(ref)


# ------------------------------------------------------------------ validation


def test_a_valid_cancel_names_the_booking() -> None:
    verdict = check_change_proposal(errand(), "B-3", [item("B-3", 24)], now=NOW, live_targets=set())
    assert not isinstance(verdict, Rejected) and verdict.booking.booking_id == "B-3"


@pytest.mark.parametrize(
    ("booking", "live", "code"),
    [
        (item("B-3", 24, "completed"), set(), "not_confirmed"),
        (item("B-3", 24, "cancelled"), set(), "not_confirmed"),
        (item("B-3", 24, "cancelled_by_provider"), set(), "not_confirmed"),
        (item("B-3", -2), set(), "already_started"),
        (item("B-3", 24), {"B-3"}, "change_in_progress"),
        (item("B-4", 24), set(), "unknown_booking"),
    ],
)
def test_a_cancel_is_refused_with_its_code(booking: BookingItem, live: set[str], code: str) -> None:
    verdict = check_change_proposal(errand(), "B-3", [booking], now=NOW, live_targets=live)
    assert isinstance(verdict, Rejected) and verdict.code == code


def test_a_booking_errand_with_a_shortlist_or_a_card_is_never_converted() -> None:
    shortlist = Shortlist(brief_version=1, window_label="x", candidates=[])
    assert convertible(errand()) is None
    refused = convertible(errand(phase=Phase.COMPARING, shortlist=shortlist))
    assert isinstance(refused, Rejected) and refused.code == "not_convertible"
    carded = convertible(errand(phase=Phase.AWAITING_APPROVAL, approval_id="ap_1"))
    assert isinstance(carded, Rejected) and carded.code == "not_convertible"
    # a cancel that was declined can be asked for again; one with a card pending cannot
    assert convertible(errand(ErrandKind.CANCEL, Phase.COMPARING)) is None
    assert convertible(errand(ErrandKind.CANCEL, Phase.AWAITING_APPROVAL)) is not None


# ------------------------------------------------------------------ the planner's side


def scripted(propose_args: dict[str, Any]):  # type: ignore[no-untyped-def]
    """A policy that lists the bookings, then proposes a cancel with the given arguments."""

    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if not view.loop:
            return tool_calls(request, "look", ("list_bookings", {}))
        name, result, is_error = view.loop[-1]
        if name == "list_bookings":
            return tool_calls(request, "propose", ("propose_cancel", propose_args))
        detail = (result or {}).get("error") if isinstance(result, dict) else None
        return answer(request, f"error: {detail}" if is_error else "ok")

    return policy


async def booking_ids(h: Harness) -> list[str]:
    return [b.booking_id for b in await h.inspector.bookings() if b.status == "confirmed"]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"booking_ref": "B-1234"}, "bad_booking_ref"),  # the model writing an id
        ({"booking_ref": "B9"}, "unknown_booking_ref"),  # a handle the view never issued
    ],
)
async def test_the_planner_cannot_name_a_booking_any_other_way(
    tmp_path: Path, args: dict[str, str], error: str
) -> None:
    async with harness(tmp_path, ScenarioConfig(id="refs", seed=1), scripted(args)) as h:
        view = await h.say("Please cancel my AC servicing.")
        rec = await h.records()
    assert view.outcome == "REPLIED"
    assert json.dumps(view.messages[-1]["body"]).count(f"error: {error}") == 1
    assert rec.approvals == [] and not [r for r in rec.op_log if r["op"] == "cancel"]


async def test_a_handle_from_an_earlier_run_is_not_valid_in_a_new_one(tmp_path: Path) -> None:
    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if "cancel" in view.current.lower() and not view.loop:
            # the second run goes straight to a handle it never listed
            if "again" in view.current.lower():
                return tool_calls(request, "reuse", ("propose_cancel", {"booking_ref": "B1"}))
            return tool_calls(request, "look", ("list_bookings", {}))
        name, result, is_error = view.loop[-1] if view.loop else ("", None, False)
        if name == "list_bookings":
            return answer(request, "Here are your bookings.")
        return answer(request, f"error: {(result or {}).get('error')}" if is_error else "ok")

    async with harness(tmp_path, ScenarioConfig(id="stale_refs", seed=1), policy) as h:
        await h.say("Show me what I could cancel.")
        view = await h.say("Cancel it again.")
    assert "unknown_booking_ref" in json.dumps(view.messages[-1]["body"])


# ------------------------------------------------------------------ the approval path


async def card_of(h: Harness) -> dict[str, Any]:
    return next(m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action")


async def test_a_cancel_card_is_what_the_user_is_asked_to_approve(tmp_path: Path) -> None:
    async with harness(tmp_path, ScenarioConfig(id="card", seed=3), reference.policy) as h:
        view = await h.say("Please cancel my AC servicing.")
        assert view.outcome == "AWAITING_APPROVAL"
        card = await card_of(h)
        body = card["body"]
        click = await h.decide(card["approval_id"], "approve", body["fingerprint"])
        done = await h.finish(click.json()["run_id"])
        rec = await h.records()
    assert body["action"] == "cancel" and body["button_label"] == "Cancel booking"
    assert [line["label"] for line in body["lines"]] == ["Provider", "Service", "When", "Booking"]
    assert body["status_line"] == "Not cancelled yet: waiting for your approval."
    assert done.outcome == "CANCELLED_VERIFIED"
    assert len([r for r in rec.op_log if r["op"] == "cancel"]) == 2  # the contract's first 503
    assert [i["action"] for i in rec.intents] == ["cancel"]
    assert rec.intents[0]["target_booking_id"] == body["material"]["booking_id"]
    assert [a["status"] for a in rec.approvals] == ["consumed"]
    for name in ("INV-01", "INV-02", "INV-04", "INV-16"):
        assert invariants.INVARIANTS[name](rec) == [], name


async def test_a_booking_the_provider_cancelled_after_the_card_is_not_cancelled_again(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, ScenarioConfig(id="moved", seed=4), reference.policy) as h:
        await h.say("Please cancel my AC servicing.")
        card = await card_of(h)
        await h.inspector.fire_provider_cancel(card["body"]["material"]["booking_id"])
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        run = await h.finish(click.json()["run_id"])
        rec = await h.records()
    assert run.outcome == "ACTION_FAILED"
    assert "not what the card showed" in run.messages[-1]["body"]["text"]
    assert not [r for r in rec.op_log if r["op"] == "cancel"]
    assert [a["status"] for a in rec.approvals] == ["invalidated"]
    assert rec.approvals[0]["invalidated_reason"] == "material_mismatch"


async def test_two_cancel_cards_for_one_booking_cancel_it_once(tmp_path: Path) -> None:
    async with harness(tmp_path, ScenarioConfig(id="twice", seed=5), reference.policy) as h:
        await h.say("Please cancel my AC servicing.")
        first = await card_of(h)
        await h.switch("second")
        await h.say("Please cancel my AC servicing.")
        second = next(
            m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action"
        )
        one = await h.decide(second["approval_id"], "approve", second["body"]["fingerprint"])
        assert (await h.finish(one.json()["run_id"])).outcome == "CANCELLED_VERIFIED"
        await h.switch("main")
        two = await h.decide(first["approval_id"], "approve", first["body"]["fingerprint"])
        late = await h.finish(two.json()["run_id"])
        rec = await h.records()
    assert late.outcome == "ACTION_FAILED"
    assert len([r for r in rec.op_log if r["op"] == "cancel" and r["outcome"] == "ok"]) == 1
    assert invariants.inv02_one_side_effect_per_approval(rec) == []


async def test_a_verified_cancel_backs_a_cancel_claim_and_nothing_else(tmp_path: Path) -> None:
    """The claim guard's records (10.8): a cancelled booking backs "I cancelled it" and not
    "I booked it", and before the cancel there is no record for either."""
    from errand.agent.claim_guard import guard
    from errand.agent.records import verified_records

    async with harness(tmp_path, ScenarioConfig(id="records", seed=6), reference.policy) as h:
        await h.say("Please cancel my AC servicing.")
        card = await card_of(h)
        deps = _deps_of(h)
        before = await verified_records(deps, set())
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        await h.finish(click.json()["run_id"])
        after = await verified_records(deps, set())
    assert [r.kind for r in before] == []
    assert [r.kind for r in after] == ["cancel"] and after[0].source_intent_id is not None
    said = guard("I cancelled your AC servicing. I also booked a plumber.", after, IST)
    assert said.text == "I cancelled your AC servicing."
    assert said.removed == ["I also booked a plumber."]
    assert guard("I cancelled it.", before, IST).text == ""


def _deps_of(h: Harness) -> Any:
    return SimpleDeps(h.manager)


class SimpleDeps:
    """The few RunDeps fields `verified_records` reads."""

    def __init__(self, manager: Any) -> None:
        self.db = manager.db
        self.user_id = "u_demo"
