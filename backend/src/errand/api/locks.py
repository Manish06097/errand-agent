"""Per-thread admission lock and the run's lease (6.6, ADR 0011).

- The `asyncio.Lock` per thread is held only for admission: the client_msg_id dedupe
  check, the active-run check, taking the lease, the interrupt check and starting the
  task. It is never held while a run executes.
- The lease row (`thread:<id>`) is held for the whole run, renewed every 5 s, and
  released in the run's `finally`. It makes a second worker process safe; within one
  process the active-run map already answers 409 first.
- Leases run on real time, never the simulated clock: they measure whether this
  process is alive, and an eval that advances simulated time by 11 minutes must not
  expire a lease mid-run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from errand.clock import to_ms, wall_now
from errand.store.agent_db import AgentDB
from errand.store.repos.leases import Lease

log = logging.getLogger(__name__)

LEASE_TTL_MS = 15_000
RENEW_EVERY_S = 5.0


def real_now_ms() -> int:
    return to_ms(wall_now())


class ThreadLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()
        return lock


def thread_key(thread_id: str) -> str:
    return f"thread:{thread_id}"


class LeaseKeeper:
    """Holds one lease: renews it in the background until released."""

    def __init__(
        self,
        db: AgentDB,
        lease: Lease,
        *,
        now_ms: Callable[[], int] = real_now_ms,
        ttl_ms: int = LEASE_TTL_MS,
        every_s: float = RENEW_EVERY_S,
    ) -> None:
        self._db = db
        self.lease = lease
        self._now_ms = now_ms
        self._ttl_ms = ttl_ms
        self._every_s = every_s
        self.lost = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def acquire(
        cls, db: AgentDB, key: str, holder: str, *, now_ms: Callable[[], int] = real_now_ms
    ) -> LeaseKeeper | None:
        async with db.session() as repos:
            lease = await repos.leases.acquire(key, holder, now_ms(), LEASE_TTL_MS)
        return None if lease is None else cls(db, lease, now_ms=now_ms)

    def start(self) -> None:
        self._task = asyncio.create_task(self._renew(), name=f"lease {self.lease.key}")

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self._every_s)
            try:
                async with self._db.session() as repos:
                    renewed = await repos.leases.renew(self.lease, self._now_ms(), self._ttl_ms)
            except Exception:
                # A failed renewal (a locked or briefly unavailable DB) is retried on the
                # next tick; the TTL is three ticks, so one miss does not lose the lease.
                log.exception("renewing lease %s failed; retrying", self.lease.key)
                continue
            if renewed is None:
                # M6 fences attempt writes on the epoch; until then a lost lease is logged.
                self.lost = True
                log.warning("lease %s was taken over (epoch %s)", self.lease.key, self.lease.epoch)
                return
            self.lease = renewed

    async def release(self) -> None:
        """Stop renewing and free the lease. Always reaches the conditional UPDATE, even
        if the renew task already died, so the thread's next message is not refused
        with run_in_progress until the TTL lapses."""
        task = self._task
        if task is not None:
            task.cancel()
            # wait() never raises the task's own exception, and still lets a cancel of
            # the caller through.
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is not None:
                log.error("lease %s renew task had failed: %r", self.lease.key, task.exception())
        async with self._db.session() as repos:
            await repos.leases.release(self.lease)


def user_key(user_id: str) -> str:
    return f"user:{user_id}"


class ConsequentialBusy(RuntimeError):
    """Another process held the user's consequential lease for too long."""


class HeldUserLock:
    """One run's hold on a user's consequential lock: the in-process lock and the lease.

    `epoch` is the lease epoch, which every attempt-state write is fenced on. `release`
    is idempotent: execute, verify and the RunManager's finally may all call it.
    """

    def __init__(self, keeper: LeaseKeeper, lock: asyncio.Lock) -> None:
        self._keeper = keeper
        self._lock = lock
        self.epoch = keeper.lease.epoch
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self._keeper.release()
        finally:
            self._lock.release()


class UserLocks:
    """The per-user consequential lock (6.6): only one execute-verify per user at a time.

    Two conversations can each hold a card for the same booking; this lock makes the
    second one's guard transaction run after the first committed, so it re-checks
    against what the first did. The in-process lock orders runs in this process; the
    `user:<id>` lease, on real time like every lease, orders processes.
    """

    def __init__(
        self,
        db: AgentDB,
        holder: str,
        *,
        attempts: int = 100,
        wait_s: float = 0.1,
    ) -> None:
        self._db = db
        self._holder = holder
        self._locks: dict[str, asyncio.Lock] = {}
        self._n = 0
        self._attempts = attempts
        self._wait_s = wait_s

    async def acquire(self, user_id: str) -> HeldUserLock:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        await lock.acquire()
        try:
            self._n += 1
            holder = f"{self._holder}:u{self._n}"
            for _ in range(self._attempts):
                keeper = await LeaseKeeper.acquire(self._db, user_key(user_id), holder)
                if keeper is not None:
                    keeper.start()
                    return HeldUserLock(keeper, lock)
                await asyncio.sleep(self._wait_s)
            raise ConsequentialBusy(f"user:{user_id} is held by another process")
        except BaseException:
            lock.release()
            raise
