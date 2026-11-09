"""M8 through the real API: the reprompt for a missing proposal, corrections that touch
the card on screen (and the race with a click), and the out-of-coverage tool rules.

The scripted planners here are the evals' reference policy with one thing changed each,
so a failure points at the code under test, not at the double.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from errand.agent.steering import REPROMPT_TEXT
from errand.approvals import service
from errand.domain.inbound import ApprovalGranted
from errand.llm.scripted import ScriptedRequest
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals import invariants
from evals.policies import reference
from evals.policies.common import answer_with_thinking, read, tool_calls
from evals.runner import Harness, RunView, harness
from tests.api.support import Gate, gated_factory

FIND = "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
BOOK = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
Policy = Callable[[ScriptedRequest], AIMessage]


def world() -> ScenarioConfig:
    return ScenarioConfig(
        id="m8", seed=8, world=WorldConfig(book=BookConfig(forced=["success"] * 3))
    )


def reprompted(request: ScriptedRequest) -> bool:
    last = request.messages[-1]
    return isinstance(last, HumanMessage) and REPROMPT_TEXT in str(last.content)


def talks_instead_of_proposing(then: str) -> Policy:
    """Does the work like the reference planner, but when it has opened the top candidate
    it says it will book (with a thinking block) instead of recommending. `then` is what
    it does once reprompted: "propose" or "talk again"."""

    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if reprompted(request):
            if then == "propose":
                search = next(r for n, r, err in view.loop if n == "search_providers" and not err)
                top = search["candidates"][0]
                return tool_calls(
                    request,
                    "The app is right, propose it.",
                    (
                        "propose_booking",
                        {"provider_id": top["provider_id"], "slot_ref": top["slots"][0]["ref"]},
                    ),
                )
            return answer_with_thinking(request, "I'll reserve S1 for you right away.")
        if view.loop and view.loop[-1][0] == "get_provider":
            return answer_with_thinking(request, "Let me book that for you.")
        return reference.policy(request)

    return policy


async def model_calls(h: Harness, run_id: str) -> list[dict[str, Any]]:
    rec = await h.records()
    return [r for r in rec.traces[run_id] if r["type"] == "model.call"]


# ---------------------------------------------------------------- reprompt (10.4)


async def test_a_reprompt_that_yields_a_proposal_ends_at_the_card(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), policy=talks_instead_of_proposing("propose")) as h:
        view = await h.say(FIND)
        calls = await model_calls(h, view.run_id)
        state = (await h.conversation())["state"]
        rec = await h.records()
    assert view.outcome == "AWAITING_APPROVAL"
    assert state["counters"]["reprompts"] == 1
    assert [m["body"]["type"] for m in view.messages][-1] in ("action", "reminder")
    # Every request after the first starts with the one before it plus what the loop
    # added, so the thinking signatures bound to that prefix stay valid; the reprompt
    # adds the assistant's own turn and exactly one user text block.
    sent = [c["request"] for c in calls]
    assert all(r["system"] == sent[0]["system"] for r in sent)
    assert all(r["tools"] == sent[0]["tools"] for r in sent)
    for before, after in itertools.pairwise(sent):
        assert after["messages"][: len(before["messages"])] == before["messages"]
    final = sent[-1]["messages"][-1]
    assert final["role"] == "user"
    assert [b["text"] for b in final["content"]] == [REPROMPT_TEXT]
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_a_second_miss_ends_no_progress_with_the_options_on_screen(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), policy=talks_instead_of_proposing("talk again")) as h:
        view = await h.say(FIND)
        convo = await h.conversation()
        op_log = await h.op_log()
        rec = await h.records()
    assert view.outcome == "MODEL_NO_PROGRESS"
    assert convo["state"]["counters"]["reprompts"] == 1  # at most one per run
    kinds = [m["body"]["type"] for m in view.messages]
    assert kinds == ["shortlist", "notice"]
    shortlist = view.messages[0]["body"]
    assert shortlist["candidates"] and shortlist["candidates"][0]["slots"]
    assert "nothing was booked" in view.messages[1]["body"]["text"]
    assert "hold_slot" not in [r["op"] for r in op_log]
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_the_user_can_still_click_a_slot_after_no_progress(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), policy=talks_instead_of_proposing("talk again")) as h:
        await h.say(FIND)
        clicked = await h.action("select_slot", {"slot_ref": "S1"})
        view = await h.finish(clicked.json()["run_id"])
    assert view.outcome == "AWAITING_APPROVAL"


@pytest.mark.parametrize(
    "text",
    [
        "Once you approve, I'll book it.",
        "I can't book anything myself; tap a slot.",
        "I'll let you pick the slot.",
        "Sunil Pipe Care looks best (S1).",
    ],
)
async def test_prose_that_is_not_about_to_book_is_not_reprompted(tmp_path: Path, text: str) -> None:
    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if view.loop and view.loop[-1][0] == "get_provider":
            return answer_with_thinking(request, text)
        return reference.policy(request)

    async with harness(tmp_path, world(), policy=policy) as h:
        view = await h.say(FIND)
        state = (await h.conversation())["state"]
    assert view.outcome == "PRESENTED_OPTIONS"
    assert state["counters"]["reprompts"] == 0


# ---------------------------------------------------------------- corrections and the card


async def to_card(h: Harness) -> dict[str, Any]:
    view = await h.say(BOOK)
    assert view.outcome == "AWAITING_APPROVAL", view.outcome
    cards = [m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action"]
    return cards[-1]


async def test_a_correction_that_keeps_the_held_slot_in_the_window_keeps_the_card(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world()) as h:
        card = await to_card(h)
        # mornings only -> any time this week: the held 09:00 slot is still inside
        view = await h.say("Actually, any time this week is fine")
        rec = await h.records()
        statuses = h.agent_db_approval_statuses()
        errand = (await h.conversation())["state"]["errand"]
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        booked = await h.finish(click.json()["run_id"])
    assert view.outcome == "AWAITING_APPROVAL"
    assert statuses == ["pending"]
    assert errand["approval_id"] == card["approval_id"] and errand["hold"] is not None
    kinds = [r["type"] for r in rec.traces[view.run_id]]
    assert "approval.invalidated" not in kinds
    assert [r["what"] for r in rec.traces[view.run_id] if r["type"] == "invalidate"] == [
        "shortlist"
    ]
    assert booked.outcome == "BOOKED_VERIFIED"


async def test_a_correction_that_moves_the_day_invalidates_the_card_and_drops_the_hold(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world()) as h:
        card = await to_card(h)
        view = await h.say("Actually, Thursday or Friday works better")
        rec = await h.records()
        errand = (await h.conversation())["state"]["errand"]
        old = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
    assert h.agent_db_approval_statuses() == ["invalidated"]
    assert errand["hold"] is None and errand["approval_id"] is None
    assert errand["phase"] == "comparing"
    assert old.status_code == 409
    traces = rec.traces[view.run_id]
    assert [r["what"] for r in traces if r["type"] == "invalidate"] == [
        "shortlist",
        "hold",
        "approval",
    ]
    update = next(r for r in traces if r["type"] == "card.update")
    assert (update["status"], update["reason"], update["changed_fields"]) == (
        "invalidated",
        "request_changed",
        ["window"],
    )
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def resume_run(h: Harness, approval_id: str) -> RunView:
    """Start the run the grant queued, the way the decide route does after a grant."""
    mgr = h.manager
    async with mgr.db.session() as repos:
        conversation = await repos.conversations.get(h.conversation_id)
    assert conversation is not None
    lease = await mgr._take_lease(h.conversation_id)
    admission = await mgr._admit(
        mgr.ids.run_id(),
        h.conversation_id,
        conversation.user_id,
        ApprovalGranted(approval_id=approval_id, via="button"),
        lease,
        trigger="approval_decision",
    )
    return await h.finish(admission.run_id)


async def test_a_grant_that_commits_before_the_correction_cannot_book_the_old_card(
    tmp_path: Path,
) -> None:
    """The click and the correction back to back, the click through the real decide route.
    The grant commits while the correction's run is in flight (the run was admitted
    first); the correction's conditional UPDATE then finds the row granted and not yet
    consumed, and wins. The click's own resume run is drained too: it finds the card
    invalidated and books nothing. The old card can no longer be booked, and no granted
    row is left behind (INV-16)."""
    gate = Gate()
    gate.open()
    async with harness(tmp_path, world(), model_factory=gated_factory(gate)) as h:
        card = await to_card(h)
        gate.opened.clear()
        gate.waiting.clear()
        posted = await h.post("Actually, Thursday or Friday works better", h.next_client_msg_id())
        await gate.waiting.wait()
        # The decide route refuses while a run is in flight (409 run_in_progress), so the
        # only way a grant lands now is the narrow race this test pins: the grant commits
        # (with its pending_resumes row) between the route's checks and the correction.
        granted = await service.grant(
            h.manager.db,
            approval_id=card["approval_id"],
            fingerprint=card["body"]["fingerprint"],
            via="button",
            message_id=None,
            resume_inbound={
                "type": "approval_granted",
                "approval_id": card["approval_id"],
                "via": "button",
            },
            now_ms=await h.now_ms(),
            policy=h.manager.approval_policy,
        )
        assert granted.outcome is service.GrantOutcome.GRANTED
        gate.open()
        view = await h.finish(posted.json()["run_id"])
        # the click's own run was queued behind the correction; drain it and look at what
        # the clicking user sees: the card is gone, nothing is booked
        resumed = await resume_run(h, card["approval_id"])
        again = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        rec = await h.records()
        book_ops = [r for r in await h.op_log() if r["op"] == "book"]
    assert h.agent_db_approval_statuses() == ["invalidated"]
    assert resumed.outcome == "REPLIED"
    assert [m["body"]["text"] for m in resumed.messages] == [
        "That card can't be used any more, so nothing was booked."
    ]
    assert (again.status_code, again.json()["error"]) == (409, "no_longer_valid")
    assert book_ops == []
    assert view.outcome == "PRESENTED_OPTIONS"
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


async def test_a_consume_that_commits_first_keeps_the_booking_path_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other order: the consume commits between the correction's read of the card and
    its UPDATE. The conditional UPDATE then loses (zero rows); the booking path stands,
    nothing is traced as invalidated, the hold stays, and the status line says what
    actually happened."""
    real = service.invalidate

    async def consumed_first(db: Any, approval_id: str, reason: str) -> bool:
        async with db.transaction() as repos:
            await repos.conn.execute(
                "UPDATE approvals SET status = 'consumed' WHERE approval_id = ?", (approval_id,)
            )
        return await real(db, approval_id, reason)

    async with harness(tmp_path, world()) as h:
        await to_card(h)
        monkeypatch.setattr(service, "invalidate", consumed_first)
        view = await h.say("Actually, Thursday or Friday works better")
        rec = await h.records()
        errand = (await h.conversation())["state"]["errand"]
    kinds = [r["type"] for r in rec.traces[view.run_id]]
    assert "approval.invalidated" not in kinds and "card.update" not in kinds
    assert h.agent_db_approval_statuses() == ["consumed"]
    assert errand["hold"] is not None and errand["phase"] == "awaiting_approval"
    status_line = view.messages[-1]["body"]["status_line"]
    assert "already approved" in status_line and "goes ahead as shown" in status_line


# ---------------------------------------------------------------- not_covered (7.3)


def not_covered_policy(field: str, words: str, service_quote: str | None = "plumber") -> Policy:
    """Reference planner, except that its first call in a turn is update_brief with a
    `not_covered` (and the plumber, when it has a quote for it)."""

    def policy(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if view.loop or words.lower() not in view.current.lower():
            return reference.policy(request)
        args = {
            "set": {
                "service": "plumber" if service_quote else None,
                "issue": None,
                "area": None,
                "window": None,
                "budget_max_inr": None,
            },
            "clear": [],
            "evidence": [{"field": "service", "quote": service_quote}] if service_quote else [],
            "not_covered": {"field": field, "words": words},
        }
        return tool_calls(request, "Record it.", ("update_brief", args))

    return policy


async def test_not_covered_naming_a_covered_alias_is_a_tool_error(tmp_path: Path) -> None:
    policy = not_covered_policy("area", "Indira Nagar")
    async with harness(tmp_path, world(), policy=policy) as h:
        view = await h.say("I need a plumber in Indira Nagar")
        calls = await model_calls(h, view.run_id)
        searched = [r for r in await h.op_log() if r["op"] == "search_providers"]
        brief = (await h.conversation())["state"]["errand"]["brief"]
    # the model is told, in the tool result, which field to set instead
    second = str(calls[1]["request"]["messages"][-1])
    assert "is_covered" in second and "indiranagar" in second and "set area instead" in second
    assert view.outcome == "ASKED_USER" and searched == []
    # the refused call recorded nothing, not even the plumber it carried
    assert brief["service"] is None and brief["version"] == 0


async def test_not_covered_words_must_be_in_the_users_message(tmp_path: Path) -> None:
    def lying(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if view.loop:
            return reference.policy(request)
        args = {
            "set": {k: None for k in ("service", "issue", "area", "window", "budget_max_inr")},
            "clear": [],
            "evidence": [],
            "not_covered": {"field": "area", "words": "Bellandur Heights"},
        }
        return tool_calls(request, "Say it.", ("update_brief", args))

    async with harness(tmp_path, world(), policy=lying) as h:
        view = await h.say("I need a plumber in HSR Layout")
        rec = await h.records()
    rejected = [r for r in rec.traces[view.run_id] if r["type"] == "guard.evidence_rejected"]
    assert len(rejected) == 1 and rejected[0]["detail"]["fields"] == ["not_covered"]
    assert view.outcome == "ASKED_USER"  # the reference planner then asks as usual


async def test_not_covered_for_a_filled_field_is_a_notice_not_a_second_ask(
    tmp_path: Path,
) -> None:
    """Area is already Indiranagar: asking for it again would break INV-13, so the
    template is a notice and the brief keeps what the user gave."""
    policy = not_covered_policy("area", "HSR Layout", service_quote=None)
    async with harness(tmp_path, world(), policy=policy) as h:
        await h.say(FIND)
        view = await h.say("Actually I am in HSR Layout")
        rec = await h.records()
        brief = (await h.conversation())["state"]["errand"]["brief"]
    assert view.outcome == "ASKED_USER"
    assert [m["body"]["type"] for m in view.messages] == ["notice"]
    assert "HSR Layout" in view.messages[0]["body"]["text"]
    assert brief["area"]["value"] == "indiranagar"
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


# ------------------------------------------------------------ widening a carried bound


async def test_any_time_widens_a_time_bound_the_user_gave_earlier(tmp_path: Path) -> None:
    """BUG-M8-4: 'not before 10' and then 'any time this week is fine' kept 'from 10:00'
    and filtered out every early slot, and the model has no other way to drop a bound."""
    async with harness(tmp_path, world()) as h:
        first = await h.say(
            "Find a plumber in Indiranagar for my kitchen tap this week, "
            "not before 10 a.m., under ₹1,500."
        )
        before = (await h.conversation())["state"]["errand"]["brief"]["window"]["value"]["label"]
        second = await h.say("Actually, any time this week is fine")
        state = (await h.conversation())["state"]["errand"]
        label = state["brief"]["window"]["value"]["label"]
    assert first.outcome != "FAILED" and second.outcome != "FAILED"
    assert before.endswith("from 10:00"), before
    assert "from 10:00" not in label, label
    slots = [
        s["start"][11:16]
        for c in (state.get("shortlist") or {}).get("candidates", [])
        for s in c.get("slots", [])
    ]
    assert any(t < "10:00" for t in slots), slots
