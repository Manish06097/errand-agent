"""Admission through the real API: dedupe before the 409, one run per thread, leases."""

from __future__ import annotations

import asyncio
from pathlib import Path

from errand.clock import to_ms, wall_now
from errand.store.agent_db import AgentDB
from evals.runner import harness
from tests.api.support import OPENER, Gate, gated_factory, world


async def test_a_retried_message_mid_run_gets_its_run_and_a_new_one_gets_409(
    tmp_path: Path,
) -> None:
    gate = Gate()
    async with harness(tmp_path, world(), model_factory=gated_factory(gate)) as h:
        first = await h.post(OPENER, "m-1")
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        await asyncio.wait_for(gate.waiting.wait(), 5)  # the run is now mid model call

        retry = await h.post(OPENER, "m-1")
        assert retry.status_code == 200
        assert retry.json() == {"run_id": run_id, "routed_as": "input"}

        other = await h.post("Indiranagar", "m-2")
        assert other.status_code == 409
        assert other.json() == {"error": "run_in_progress", "run_id": run_id}

        gate.open()
        view = await h.finish(run_id)
        assert view.outcome == "ASKED_USER"
        convo = await h.conversation()
        users = [m for m in convo["messages"] if m["role"] == "user"]
        assert [m["run_id"] for m in users] == [run_id]
        # After the run, the refused message can be sent again.
        again = await h.post("Indiranagar", "m-2")
        assert again.status_code == 202
        await h.finish(again.json()["run_id"])


async def test_concurrent_posts_of_one_message_start_one_run(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        responses = await asyncio.gather(*[h.post(OPENER, "m-dup") for _ in range(4)])
        assert sorted(r.status_code for r in responses) == [200, 200, 200, 202]
        run_ids = {r.json()["run_id"] for r in responses}
        assert len(run_ids) == 1
        view = await h.finish(run_ids.pop())
        assert [e.event for e in view.events].count("run.started") == 1


async def test_a_message_to_an_unknown_conversation_is_404(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        response = await h.client.post(
            "/api/conversations/c_9999/messages", json={"client_msg_id": "x", "text": "hi"}
        )
        assert response.status_code == 404


async def test_a_lease_taken_over_after_expiry_bumps_the_epoch(tmp_path: Path) -> None:
    db = await AgentDB.open(tmp_path / "agent.sqlite")
    now = to_ms(wall_now())
    try:
        async with db.session() as repos:
            a = await repos.leases.acquire("thread:c_1", "worker-a", now, 15_000)
            assert a is not None and a.epoch == 1
            # Live and held by someone else: refused.
            assert await repos.leases.acquire("thread:c_1", "worker-b", now + 5_000, 15_000) is None
            # Expired: taken over, with a new epoch.
            b = await repos.leases.acquire("thread:c_1", "worker-b", now + 15_000, 15_000)
            assert b is not None and (b.holder, b.epoch) == ("worker-b", 2)
            # The old holder can neither renew nor release what it lost.
            assert await repos.leases.renew(a, now + 16_000, 15_000) is None
            assert await repos.leases.release(a) is False
            assert await repos.leases.release(b) is True
            # A released lease is free at once, and the next holder bumps the epoch again.
            c = await repos.leases.acquire("thread:c_1", "worker-c", now + 16_000, 15_000)
            assert c is not None and c.epoch == 3
    finally:
        await db.close()
