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
    # Refused by the consume itself: the row was no longer granted.
    refusals = [
        r["detail"] for r in rec.traces[loser] if r["type"] == "guard.planner_tool_rejected"
    ]
    assert refusals == [{"tool": "book", "reason": "approval consumed: not consumable"}]
    assert [a["status"] for a in rec.approvals] == ["consumed"]
    assert invariants.inv02_one_side_effect_per_approval(rec) == []
    assert invariants.inv01_side_effects_match_a_consumed_approval(rec) == []
