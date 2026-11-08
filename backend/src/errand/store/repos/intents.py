"""Write-ahead intents and their attempts (architecture 9.2).

An intent is the user-approved action (one per consumed approval, `UNIQUE(approval_id)`);
an attempt is one provider-side try with its own idempotency key. Attempt-state writes
are conditional on the state they leave and on `fence_epoch`, the user lease epoch the
attempt was started under, so a worker whose lease was taken over cannot overwrite the
new holder's writes (M6 is the first milestone that can take a lease over; the fence is
in place from the start).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

IntentState = Literal["open", "succeeded", "failed", "needs_attention"]
AttemptState = Literal["prepared", "in_flight", "unknown", "applied", "not_applied"]
UNRESOLVED: tuple[IntentState, ...] = ("open", "needs_attention")
_INTENT = (
    "intent_id, conversation_id, errand_id, user_id, action, approval_id, fingerprint, "
    "material_json, target_booking_id, replaces_booking_id, same_key_retries, state, "
    "result_json, created_at, updated_at"
)
_ATTEMPT = (
    "attempt_id, intent_id, n, hold_id, hold_expires_at, idempotency_key, state, calls, "
    "status_checks, last_error, fence_epoch, created_at, updated_at"
)


@dataclass(frozen=True)
class IntentRow:
    intent_id: str
    conversation_id: str
    errand_id: str
    user_id: str
    action: str
    approval_id: str
    fingerprint: str
    material_json: str
    target_booking_id: str | None
    replaces_booking_id: str | None
    same_key_retries: int
    state: IntentState
    result_json: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    intent_id: str
    n: int
    hold_id: str | None
    hold_expires_at: int | None
    idempotency_key: str
    state: AttemptState
    calls: int
    status_checks: int
    last_error: str | None
    fence_epoch: int
    created_at: int
    updated_at: int


def _intent(row: Any) -> IntentRow:
    return IntentRow(*(row[i] for i in range(15)))


def _attempt(row: Any) -> AttemptRow:
    return AttemptRow(*(row[i] for i in range(13)))


def _marks(values: Sequence[str]) -> str:
    return ", ".join("?" * len(values))


class IntentsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert_intent(self, row: IntentRow) -> None:
        await self._conn.execute(
            f"INSERT INTO action_intents ({_INTENT}) VALUES ({_marks(['?'] * 15)})",
            (
                row.intent_id,
                row.conversation_id,
                row.errand_id,
                row.user_id,
                row.action,
                row.approval_id,
                row.fingerprint,
                row.material_json,
                row.target_booking_id,
                row.replaces_booking_id,
                row.same_key_retries,
                row.state,
                row.result_json,
                row.created_at,
                row.updated_at,
            ),
        )

    async def insert_attempt(self, row: AttemptRow) -> None:
        await self._conn.execute(
            f"INSERT INTO intent_attempts ({_ATTEMPT}) VALUES ({_marks(['?'] * 13)})",
            (
                row.attempt_id,
                row.intent_id,
                row.n,
                row.hold_id,
                row.hold_expires_at,
                row.idempotency_key,
                row.state,
                row.calls,
                row.status_checks,
                row.last_error,
                row.fence_epoch,
                row.created_at,
                row.updated_at,
            ),
        )

    async def get(self, intent_id: str) -> IntentRow | None:
        async with self._conn.execute(
            f"SELECT {_INTENT} FROM action_intents WHERE intent_id = ?", (intent_id,)
        ) as cur:
            row = await cur.fetchone()
        return _intent(row) if row else None

    async def get_by_approval(self, approval_id: str) -> IntentRow | None:
        async with self._conn.execute(
            f"SELECT {_INTENT} FROM action_intents WHERE approval_id = ?", (approval_id,)
        ) as cur:
            row = await cur.fetchone()
        return _intent(row) if row else None

    async def unresolved_for_user(self, user_id: str) -> list[IntentRow]:
        async with self._conn.execute(
            f"SELECT {_INTENT} FROM action_intents WHERE user_id = ? "
            "AND state IN ('open', 'needs_attention') ORDER BY created_at, rowid",
            (user_id,),
        ) as cur:
            return [_intent(r) for r in await cur.fetchall()]

    async def attempts(self, intent_id: str) -> list[AttemptRow]:
        async with self._conn.execute(
            f"SELECT {_ATTEMPT} FROM intent_attempts WHERE intent_id = ? ORDER BY n",
            (intent_id,),
        ) as cur:
            return [_attempt(r) for r in await cur.fetchall()]

    async def get_attempt(self, attempt_id: str) -> AttemptRow | None:
        async with self._conn.execute(
            f"SELECT {_ATTEMPT} FROM intent_attempts WHERE attempt_id = ?", (attempt_id,)
        ) as cur:
            row = await cur.fetchone()
        return _attempt(row) if row else None

    async def move_attempt(
        self,
        attempt_id: str,
        *,
        frm: Sequence[AttemptState],
        to: AttemptState,
        fence_epoch: int,
        now_ms: int,
        add_calls: int = 0,
        add_status_checks: int = 0,
        last_error: str | None = None,
    ) -> bool:
        """Conditional on the states it leaves and on the attempt's fence epoch."""
        cur = await self._conn.execute(
            "UPDATE intent_attempts SET state = ?, calls = calls + ?, "
            "status_checks = status_checks + ?, last_error = COALESCE(?, last_error), "
            f"updated_at = ? WHERE attempt_id = ? AND fence_epoch = ? AND state IN ({_marks(frm)})",
            (to, add_calls, add_status_checks, last_error, now_ms, attempt_id, fence_epoch, *frm),
        )
        return cur.rowcount == 1

    async def resolve_intent(
        self,
        intent_id: str,
        *,
        frm: Sequence[IntentState],
        to: IntentState,
        result_json: str | None,
        now_ms: int,
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE action_intents SET state = ?, result_json = COALESCE(?, result_json), "
            f"updated_at = ? WHERE intent_id = ? AND state IN ({_marks(frm)})",
            (to, result_json, now_ms, intent_id, *frm),
        )
        return cur.rowcount == 1
