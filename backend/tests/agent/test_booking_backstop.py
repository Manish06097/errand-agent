"""S28's backstop alone: two executes of one granted approval with every lock removed.

The thread lock and the user's consequential lock normally serialise these, so the
second never gets near the guard transaction. Here both are bypassed (two runs admitted
straight into the RunManager, a no-op consequential lock) and both are held at the door
of the guard transaction until both have passed re-entry and preflight. What is left is
the conditional consume (`AND status = 'granted'`, exactly one row) and
`UNIQUE(approval_id)` on intents: one `book`, one booking, and the second run ends
cleanly ("already approved") rather than crashing on a constraint.

The second run is admitted once the first is inside execute, so it enters the thread
from the first run's latest checkpoint as plain input, like the spike's second resume
(L4). Admitting both as resumes of the same parked checkpoint makes LangGraph itself
refuse the second (two updates to one channel in one step), which would test LangGraph
rather than our backstop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from errand.agent.nodes import execute as execute_node
from errand.api.locks import LeaseKeeper, thread_key
from errand.approvals import service
from errand.domain.inbound import ApprovalGranted
from errand.store.repos.intents import IntentsRepo
from errand.store.repos.leases import Lease
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals import invariants
from evals.runner import harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


class NoLock:
    epoch = 1

    async def release(self) -> None:
        return None


class NoLocks:
    async def acquire(self, user_id: str) -> NoLock:
        return NoLock()


async def test_two_unlocked_executes_of_one_approval_book_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = ScenarioConfig(
        id="backstop", seed=28, world=WorldConfig(book=BookConfig(forced=["success"] * 2))
    )
    real_consume = service.consume
    door = asyncio.Barrier(2)
    first_in = asyncio.Event()

    async def held_consume(*args: Any, **kwargs: Any) -> Any:
        first_in.set()
        await asyncio.wait_for(door.wait(), 10)  # both runs are past re-entry and preflight
        return await real_consume(*args, **kwargs)

    monkeypatch.setattr(execute_node.service, "consume", held_consume)
    async with harness(tmp_path, world, consequential=NoLocks()) as h:
        view = await h.say(OPENER)
        assert view.outcome == "AWAITING_APPROVAL"
        convo = await h.conversation()
        card = next(m for m in convo["messages"] if m["body"].get("type") == "action")
        aid, fp = card["approval_id"], card["body"]["fingerprint"]
        granted = await service.grant(
            h.manager.db,
            approval_id=aid,
            fingerprint=fp,
            via="button",
            message_id=None,
            resume_inbound={"type": "approval_granted", "approval_id": aid, "via": "button"},
            now_ms=await h.now_ms(),
            policy=h.manager.approval_policy,
        )
        assert granted.outcome is service.GrantOutcome.GRANTED
        manager = h.manager
        user_id = "u_demo"
        run_ids = []
        for n in (1, 2):
            # Straight past admission: no thread lock, no active-run check, a lease
            # object that nobody else knows about.
            lease = LeaseKeeper(
                manager.db, Lease(thread_key(h.conversation_id), f"bypass-{n}", 1, 0)
            )
            admitted = await manager._admit(
                manager.ids.run_id(),
                h.conversation_id,
                user_id,
                ApprovalGranted(approval_id=aid, via="button"),
                lease,
                trigger="approval_decision",
            )
            run_ids.append(admitted.run_id)
            if n == 1:
                await asyncio.wait_for(first_in.wait(), 10)
        await manager.wait_idle()
        rec = await h.records()
    outcomes = sorted(r["outcome"] for r in rec.runs if r["run_id"] in run_ids)
    assert outcomes == ["BOOKED_VERIFIED", "REPLIED"], outcomes
    assert sum(1 for r in rec.op_log if r["op"] == "book") == 1
    made = [b for b in rec.world_bookings if b["created_by_key"] is not None]
    assert len(made) == 1
    loser = next(r["run_id"] for r in rec.runs if r["outcome"] == "REPLIED")
    assert not [r for r in rec.traces[loser] if r["type"] == "tool.call" and r["tool"] == "book"]
    # Refused by the consume itself: the row was no longer granted. An executor-side
    # refusal has its own record, and it is not a planner rejection.
    refusals = [r for r in rec.traces[loser] if r["type"] == "approval.consume_refused"]
    assert [(r["approval_id"], r["reason"], r["status"]) for r in refusals] == [
        (aid, "not_consumable", "consumed")
    ]
    assert not [r for r in rec.traces[loser] if r["type"] == "guard.planner_tool_rejected"]
    assert [a["status"] for a in rec.approvals] == ["consumed"]
    assert invariants.inv02_one_side_effect_per_approval(rec) == []
    assert invariants.inv01_side_effects_match_a_consumed_approval(rec) == []


async def test_a_fenced_off_attempt_is_an_outcome_and_a_prepared_one_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt cannot move to in_flight (another holder owns its epoch): that is a
    recorded PENDING_VERIFICATION, not an AssertionError, and nothing is sent. The attempt
    stays `prepared`; when the approval is run again (a crash resume) re-entry closes it
    as never sent instead of handing it to verify, which would skip it (8.7, M6 owns
    sending it once with the same key)."""
    world = ScenarioConfig(
        id="fenced", seed=28, world=WorldConfig(book=BookConfig(forced=["success"] * 2))
    )
    async with harness(tmp_path, world) as h:
        view = await h.say(OPENER)
        assert view.outcome == "AWAITING_APPROVAL"
        card = next(
            m for m in (await h.conversation())["messages"] if m["body"]["type"] == "action"
        )
        aid, fp = card["approval_id"], card["body"]["fingerprint"]
        real = IntentsRepo.move_attempt
        calls = {"n": 0}

        async def fenced(self: Any, *args: Any, **kwargs: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 1 and kwargs.get("to") == "in_flight":
                return False
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(IntentsRepo, "move_attempt", fenced)
        click = await h.decide(aid, "approve", fp)
        first = await h.finish(click.json()["run_id"])
        assert first.outcome == "PENDING_VERIFICATION"
        assert not [r for r in await h.op_log() if r["op"] == "book"]
        async with h.manager.db.session() as repos:
            intent = await repos.intents.get_by_approval(aid)
            assert intent is not None
            attempts = await repos.intents.attempts(intent.intent_id)
        assert [a.state for a in attempts] == ["prepared"]

        lease = LeaseKeeper(h.manager.db, Lease(thread_key(h.conversation_id), "resume-1", 2, 0))
        admitted = await h.manager._admit(
            h.manager.ids.run_id(),
            h.conversation_id,
            "u_demo",
            ApprovalGranted(approval_id=aid, via="button"),
            lease,
            trigger="approval_decision",
        )
        await h.manager.wait_idle()
        async with h.manager.db.session() as repos:
            intent = await repos.intents.get_by_approval(aid)
            assert intent is not None
            attempts = await repos.intents.attempts(intent.intent_id)
        rec = await h.records()
    outcome = next(r["outcome"] for r in rec.runs if r["run_id"] == admitted.run_id)
    assert outcome == "ACTION_FAILED", outcome
    assert [a.state for a in attempts] == ["not_applied"] and intent.state == "failed"
    assert not [r for r in rec.op_log if r["op"] == "book"]
