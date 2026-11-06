"""BUG-M3-4: a restarted API took a dead run's live lease as its own, without an epoch bump.

Since BUG-M3-2 the lease holder names the admission, `api-<pid>:a<n>`. Both parts
repeat across a restart: the admission counter starts at 1 again, and the pid is the
same whenever the OS hands it out again (every restart, in a container). The lease
acquire treats an equal holder as "already ours", so the restarted process took a
lease that a dead run still held, before it expired, and kept its epoch. M6 fences
attempt-state writes on that epoch, so two owners would share one.

The test does what the lead did through the API: a run ends, the process is taken to
have died mid-run (its lease is put back to unexpired), and a new app on the same data
dir, in the same process and so with the same pid, takes the next message. It must be
refused while the lease is live, and once it lapses the takeover must bump the epoch.
"""

from __future__ import annotations

from pathlib import Path

from errand.api.locks import LEASE_TTL_MS, thread_key
from errand.clock import to_ms, wall_now
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig
from tests.regressions.test_bug_m3_2_sim_run_ids_reused_after_restart import Api, api


async def _lease(a: Api, key: str) -> tuple[str, int, int]:
    async with a.manager.db.session() as repos:
        lease = await repos.leases.get(key)
    assert lease is not None
    return lease.holder, lease.epoch, lease.expires_at


async def _set_expiry(a: Api, key: str, expires_at: int) -> None:
    async with a.manager.db.session() as repos:
        await repos.conn.execute(
            "UPDATE leases SET expires_at = ? WHERE key = ?", (expires_at, key)
        )


async def test_a_restart_does_not_take_a_dead_runs_live_lease(tmp_path: Path) -> None:
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(ScenarioConfig(id="bug_m3_4", seed=7), "sim")
    try:
        async with api(tmp_path, host) as a:
            created = await a.client.post("/api/conversations")
            conversation = created.json()["conversation_id"]
            first = await a.post(conversation, "need a plumber", "m-1")
            assert first.status_code == 202
            await a.finish(first.json()["run_id"])

            # The process dies mid-run: its lease is still live when the next one starts.
            key = thread_key(conversation)
            dead_holder, dead_epoch, _ = await _lease(a, key)
            await _set_expiry(a, key, to_ms(wall_now()) + LEASE_TTL_MS)

        async with api(tmp_path, host) as b:
            refused = await b.post(conversation, "indiranagar", "m-2")
            assert refused.status_code == 409, refused.text
            assert refused.json() == {"error": "run_in_progress", "run_id": None}
            holder, epoch, _ = await _lease(b, key)
            assert (holder, epoch) == (dead_holder, dead_epoch)

            # The dead run's lease lapses; the client retries and this process takes over.
            await _set_expiry(b, key, to_ms(wall_now()) - 1)
            retried = await b.post(conversation, "indiranagar", "m-2")
            assert retried.status_code == 202, retried.text
            holder, epoch, _ = await _lease(b, key)
            assert holder != dead_holder
            assert epoch == dead_epoch + 1
            await b.finish(retried.json()["run_id"])
    finally:
        host.close()


async def test_two_processes_with_the_same_pid_never_share_a_holder(tmp_path: Path) -> None:
    # Each app's first admission is a1; the holder must still tell them apart.
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(ScenarioConfig(id="bug_m3_4_holders", seed=7), "sim")
    holders: list[str] = []
    try:
        for n in range(2):
            async with api(tmp_path, host) as a:
                created = await a.client.post("/api/conversations")
                conversation = created.json()["conversation_id"]
                posted = await a.post(conversation, "need a plumber", f"m-{n}")
                assert posted.status_code == 202
                await a.finish(posted.json()["run_id"])
                holder, epoch, _ = await _lease(a, thread_key(conversation))
                assert epoch == 1
                holders.append(holder)
    finally:
        host.close()
    assert holders[0] != holders[1]
