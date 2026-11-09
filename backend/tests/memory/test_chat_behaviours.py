"""The behaviours of the scenarios the scope trim cut (S16b, S32b, S33, S34b, S35b, S42b/c,
S61), proved at API level: each test drives the real HTTP routes through the scenario
runner with a scripted planner, and asserts on rows, chips and approvals.

- the update_memory chip: Update corrects, "Just this time" leaves memory alone (S33);
- propose_forget only renders a chip, a forget needs the click (S61);
- the offer chip saves through save_memory with ui_action evidence, and refuses after a
  forget redacted the span it points at;
- Keep and Dismiss on a suggestion chip, 409 with the current row on a second click;
- a forget invalidates a pending card that listed the row, and leaves a consumed one alone;
- a failing run_inference leaves the outcome and the single run.end unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.dsl import Scenario
from evals.runner import ScenarioResult, run_scenario

BOOK = "Book a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def scenario(sid: str, steps: list[dict[str, Any]], **extra: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": sid,
            "title": sid,
            "category": ["memory"],
            "model": "scripted:memory",
            "seed": 35,
            "user": {"persona": "cooperative", "goal": {}},
            "steps": steps,
            "expect": {"invariants": ["default"]},
            **extra,
        }
    )


async def passes(tmp_path: Path, sc: Scenario) -> ScenarioResult:
    result = await run_scenario(sc, tmp_path)
    assert result.passed, "\n".join(result.failures)
    return result


async def test_update_chip_update_corrects_and_just_this_time_leaves_memory_alone(
    tmp_path: Path,
) -> None:
    ask = "Book a plumber in Koramangala for my kitchen tap this week, mornings only, under ₹1,500."
    update = scenario(
        "chat_update",
        [
            {"say": ask},
            {"expect_chip": {"type": "memory_update", "count": 1}},
            {"memory_action": {"kind": "update_memory", "chip": "memory_update"}},
            {"expect_memory": {"key": "home_area", "status": "active", "value": "koramangala"}},
            {"expect_memory": {"key": "home_area", "status": "superseded", "count": 1}},
        ],
        memory_seed="memory_demo",
        world={"book": {"forced": ["success"]}},
    )
    await passes(tmp_path / "update", update)
    just_once = scenario(
        "chat_just_this_time",
        [
            {"say": ask},
            {"expect_chip": {"type": "memory_update", "count": 1}},
            {
                "memory_action": {
                    "kind": "update_memory",
                    "chip": "memory_update",
                    "choice": "just_this_time",
                }
            },
            {"expect_memory": {"key": "home_area", "status": "active", "value": "indiranagar"}},
            {"expect_memory": {"key": "home_area", "status": "superseded", "count": 0}},
        ],
        memory_seed="memory_demo",
    )
    await passes(tmp_path / "once", just_once)


async def test_propose_forget_renders_a_chip_and_only_the_click_forgets(tmp_path: Path) -> None:
    sc = scenario(
        "chat_propose_forget",
        [
            {"say": "Please forget my home area."},
            {"expect_chip": {"type": "memory_forget", "count": 1}},
            {"expect_memory": {"key": "home_area", "status": "active", "count": 1}},
            {"expect_memory": {"key": "home_area", "status": "forgotten", "count": 0}},
            {"memory_action": {"kind": "confirm_forget", "chip": "memory_forget", "again": True}},
            {"expect_memory": {"key": "home_area", "status": "forgotten", "count": 1}},
        ],
        memory_seed="memory_demo",
    )
    await passes(tmp_path, sc)


async def test_the_offer_chip_saves_with_ui_action_evidence(tmp_path: Path) -> None:
    sc = scenario(
        "chat_offer_saves",
        [
            {"say": BOOK + " (save my budget)"},
            {"expect_chip": {"type": "memory_offer", "count": 1}},
            {"expect_memory": {"key": "budget_ceiling", "scope": "service:plumber", "count": 0}},
            {"memory_action": {"kind": "save_memory", "chip": "memory_offer"}},
            {
                "expect_memory": {
                    "key": "budget_ceiling",
                    "scope": "service:plumber",
                    "status": "active",
                    "kind": "explicit",
                    "evidence_type": "ui_action",
                    "value": 1500,
                }
            },
        ],
    )
    await passes(tmp_path, sc)


async def test_the_offer_chip_refuses_after_a_forget_redacted_its_span(tmp_path: Path) -> None:
    """The electrician's saved 1500 is forgotten; the redaction of its surface forms
    overwrites "₹1,500" in the user's message, so the chip has nothing left to save."""
    sc = scenario(
        "chat_offer_redacted",
        [
            {"say": BOOK + " (save my budget)"},
            {"expect_chip": {"type": "memory_offer", "count": 1}},
            {"forget_memory": {"key": "budget_ceiling", "scope": "service:electrician"}},
            {
                "memory_action": {
                    "kind": "save_memory",
                    "chip": "memory_offer",
                    "expect_status": 409,
                }
            },
            {"expect_memory": {"key": "budget_ceiling", "scope": "service:plumber", "count": 0}},
        ],
        memory_seed="memory_demo",
    )
    await passes(tmp_path, sc)


async def _two_bookings(extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"say": "Book a plumber in Indiranagar for my kitchen tap tomorrow morning, under ₹1,500."},
        {"until": "approval_card"},
        {"approve": {}},
        {"say": "Book the same plumber as last time."},
        {"expect_outcome": "ASKED_USER"},
        {"answer": {}},
        {"expect_outcome": "AWAITING_APPROVAL"},
        {"approve": {}},
        {"expect_outcome": "BOOKED_VERIFIED"},
        {"expect_chip": {"type": "memory_suggestion", "count": 1}},
        *extra,
    ]


@pytest.mark.parametrize("action", ["keep_memory", "dismiss"])
async def test_keep_and_dismiss_answer_409_on_a_second_click(tmp_path: Path, action: str) -> None:
    status = "active" if action == "keep_memory" else "rejected"
    steps = await _two_bookings(
        [
            {"memory_action": {"kind": action, "chip": "memory_suggestion", "again": True}},
            {
                "expect_memory": {
                    "key": "preferred_provider",
                    "scope": "service:plumber",
                    "status": status,
                    "count": 1,
                }
            },
        ]
    )
    sc = scenario(
        "chat_" + action,
        steps,
        world={"book": {"forced": ["success", "success"]}},
        user={"persona": "cooperative", "goal": {"window": "tomorrow morning"}},
    )
    await passes(tmp_path, sc)


async def test_a_forget_invalidates_a_pending_card_and_leaves_a_consumed_one(
    tmp_path: Path,
) -> None:
    pending = scenario(
        "chat_forget_pending",
        [
            {"say": BOOK},
            {"until": "approval_card"},
            {"expect_approvals": ["pending"]},
            {"forget_memory": {"key": "home_area"}},
            {"expect_approvals": ["invalidated:request_changed"]},
        ],
        memory_seed="memory_demo",
        world={"book": {"forced": ["success"]}},
    )
    await passes(tmp_path / "pending", pending)
    consumed = scenario(
        "chat_forget_consumed",
        [
            {"say": BOOK},
            {"until": "approval_card"},
            {"approve": {}},
            {"expect_outcome": "BOOKED_VERIFIED"},
            {"expect_approvals": ["consumed"]},
            {"forget_memory": {"key": "home_area"}},
            {"expect_approvals": ["consumed"]},
        ],
        memory_seed="memory_demo",
        world={"book": {"forced": ["success"]}},
    )
    await passes(tmp_path / "consumed", consumed)


async def test_a_failing_inference_leaves_the_outcome_and_the_single_run_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from errand.memory.service import MemoryService

    async def boom(self: MemoryService, **kwargs: Any) -> list[Any]:
        raise RuntimeError("inference is down")

    monkeypatch.setattr(MemoryService, "run_inference", boom)
    seen: list[Any] = []
    sc = scenario(
        "chat_inference_fails",
        [
            {"say": BOOK},
            {"until": "approval_card"},
            {"approve": {}},
            {"expect_outcome": "BOOKED_VERIFIED"},
        ],
        memory_seed="memory_demo",
        world={"book": {"forced": ["success"]}},
    )
    result = await run_scenario(sc, tmp_path, on_records=seen.append)
    assert result.passed, "\n".join(result.failures)
    records = seen[0]
    for _run, trace in records.run_records():
        assert sum(1 for r in trace if r["type"] == "run.end") == 1
    errors = [r for r in records.all_records() if r["type"] == "error"]
    assert any(e["where"] == "memory.run_inference" for e in errors)


async def test_a_card_cannot_be_created_from_a_memory_that_is_no_longer_eligible(
    tmp_path: Path,
) -> None:
    """prepare_proposal lists the memory ids behind the brief; the insert checks, in its own
    transaction, that each is still eligible (decision 11)."""
    from errand.approvals import service
    from errand.store.repos.approvals import ApprovalRow
    from evals.runner import harness
    from tests.api.support import world

    def row(n: int) -> ApprovalRow:
        return ApprovalRow(
            f"ap_{n}", "c_0001", "e_1", f"pr_{n}", "book", f"fp{n}", "{}", "{}", None,
            "pending", None, None, None, None, 1000, None, None, 10_000_000,
        )  # fmt: skip

    policy = service.ApprovalPolicy(ttl_ms=600_000, grace_ms=20_000)
    async with harness(tmp_path, world(), memory_seed="memory_demo") as h:
        db = h.app.state.manager.db
        conversation = h.conversation_id
        keep = ApprovalRow(**{**row(1).__dict__, "conversation_id": conversation})
        ok = await service.create(db, keep, policy, ["m-01"])
        assert ok.created and not ok.stale_memory
        gone = await h.client.delete("/api/memory/m-01", params={"client_action_id": "d1"})
        assert gone.status_code == 200
        # the pending card that listed m-01 was invalidated by the forget itself
        async with db.session() as repos:
            assert (await repos.approvals.get("ap_1")).status == "invalidated"
        late = ApprovalRow(**{**row(2).__dict__, "conversation_id": conversation})
        refused = await service.create(db, late, policy, ["m-01"])
        assert not refused.created and refused.stale_memory == ("m-01",)
        async with db.session() as repos:
            assert await repos.approvals.get("ap_2") is None
