"""Lease rows: `thread:<id>` and `user:<id>` locks that work across processes (6.6).

A lease is taken with one conditional statement: it succeeds if the row is free or
has expired, and every takeover by a new holder increments `epoch`, which later fences
attempt-state writes (M6). Renewal and release are conditional on holder and epoch, so
a holder that lost its lease cannot extend or drop someone else's.

Leases measure process liveness, so callers pass real time (`errand.clock.wall_now`),
never a simulated clock: an eval that advances the simulated clock by 11 minutes must
not expire a lease that a live process is still renewing.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class Lease:
    key: str
    holder: str
    epoch: int
    expires_at: int


class LeasesRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def acquire(self, key: str, holder: str, now_ms: int, ttl_ms: int) -> Lease | None:
        """Take the lease if it is free, expired or already ours; None if someone holds it."""
        await self._conn.execute(
            "INSERT INTO leases (key, holder, epoch, expires_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "epoch = CASE WHEN leases.holder = excluded.holder THEN leases.epoch "
            "ELSE leases.epoch + 1 END, "
            "holder = excluded.holder, expires_at = excluded.expires_at "
            "WHERE leases.expires_at <= ? OR leases.holder = excluded.holder",
            (key, holder, now_ms + ttl_ms, now_ms),
        )
        lease = await self.get(key)
        if lease is None or lease.holder != holder:
            return None
        return lease

    async def take_over(self, key: str) -> None:
        """Startup takeover: whoever held `key` is dead (one API process per data
        directory), so bump the epoch and expire the row without waiting for the TTL. The
        next `acquire` is then an ordinary one by a new holder and bumps the epoch again,
        so anything the dead holder fenced is fenced out for good."""
        await self._conn.execute(
            "UPDATE leases SET epoch = epoch + 1, expires_at = 0 WHERE key = ?", (key,)
        )

    async def renew(self, lease: Lease, now_ms: int, ttl_ms: int) -> Lease | None:
        cur = await self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE key = ? AND holder = ? AND epoch = ?",
            (now_ms + ttl_ms, lease.key, lease.holder, lease.epoch),
        )
        if cur.rowcount != 1:
            return None
        return Lease(lease.key, lease.holder, lease.epoch, now_ms + ttl_ms)

    async def release(self, lease: Lease) -> bool:
        # The row stays, with its epoch, so the next holder's takeover increments it.
        cur = await self._conn.execute(
            "UPDATE leases SET expires_at = 0 WHERE key = ? AND holder = ? AND epoch = ?",
            (lease.key, lease.holder, lease.epoch),
        )
        return cur.rowcount == 1

    async def get(self, key: str) -> Lease | None:
        async with self._conn.execute(
            "SELECT key, holder, epoch, expires_at FROM leases WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return Lease(row["key"], row["holder"], row["epoch"], row["expires_at"]) if row else None
