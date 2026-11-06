"""LeaseKeeper: a failing renewal is retried, and release always frees the row.

If release() stopped at a dead renew task, the lease would stay live until its TTL
lapsed and the thread's next message would get 409 run_in_progress for no reason.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from errand.api.locks import LEASE_TTL_MS, LeaseKeeper
from errand.clock import to_ms, wall_now
from errand.store.agent_db import AgentDB

KEY = "thread:c_1"


async def test_release_frees_the_lease_even_if_the_renew_task_died(tmp_path: Path) -> None:
    db = await AgentDB.open(tmp_path / "agent.sqlite")
    try:
        keeper = await LeaseKeeper.acquire(db, KEY, "worker-a")
        assert keeper is not None

        async def died() -> None:
            raise RuntimeError("database is locked")

        keeper._task = asyncio.create_task(died())
        await asyncio.sleep(0)
        assert keeper._task.done()

        await keeper.release()

        now = to_ms(wall_now())
        async with db.session() as repos:
            row = await repos.leases.get(KEY)
            assert row is not None and row.expires_at == 0
            # Free at once for the next holder, well inside what was the TTL.
            other = await repos.leases.acquire(KEY, "worker-b", now, LEASE_TTL_MS)
        assert other is not None and other.epoch == 2
    finally:
        await db.close()


async def test_a_renewal_that_raises_is_retried_and_release_still_works(tmp_path: Path) -> None:
    db = await AgentDB.open(tmp_path / "agent.sqlite")
    calls = 0

    def flaky_now() -> int:
        nonlocal calls
        calls += 1
        if calls in (2, 3):  # the first two renewals (call 1 is acquire)
            raise OSError("disk I/O error")
        return to_ms(wall_now())

    try:
        keeper = await LeaseKeeper.acquire(db, KEY, "worker-a", now_ms=flaky_now)
        assert keeper is not None
        keeper._every_s = 0.01
        first_expiry = keeper.lease.expires_at
        keeper.start()
        for _ in range(200):
            if calls >= 5:
                break
            await asyncio.sleep(0.01)
        assert keeper._task is not None and not keeper._task.done()
        assert keeper.lease.expires_at >= first_expiry  # renewed after the failures
        assert calls >= 5 and not keeper.lost

        await keeper.release()

        async with db.session() as repos:
            row = await repos.leases.get(KEY)
        assert row is not None and row.expires_at == 0
    finally:
        await db.close()
