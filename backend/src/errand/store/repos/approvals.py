"""Approval rows and pending resumes (architecture 8.2, 9.7).

Plain SQL, no policy: `errand.approvals.service` decides when each statement runs and
always inside a `BEGIN IMMEDIATE` transaction. Every status change here is a
conditional UPDATE whose WHERE clause names the states it may leave, so a row can
never be moved out of a state the caller did not expect. Every statement that ends a
live approval names `granted` as well as `pending`, because the partial unique index
`one_live_approval` covers both.

Timestamps are integer epoch ms on the agent clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

ApprovalStatus = Literal["pending", "granted", "consumed", "declined", "invalidated", "expired"]
LIVE: tuple[ApprovalStatus, ...] = ("pending", "granted")
_COLUMNS = (
    "approval_id, conversation_id, errand_id, proposal_id, action, fingerprint, material_json, "
    "card_json, bound_hold_id, status, granted_via, granted_message_id, invalidated_reason, "
    "consumed_by_intent, requested_at, granted_at, consumed_at, expires_at, issued_hold_id"
)


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: str
    conversation_id: str
    errand_id: str
    proposal_id: str
    action: str
    fingerprint: str
    material_json: str
    card_json: str
    bound_hold_id: str | None
    status: ApprovalStatus
    granted_via: str | None
    granted_message_id: str | None
    invalidated_reason: str | None
    consumed_by_intent: str | None
    requested_at: int
    granted_at: int | None
    consumed_at: int | None
    expires_at: int
    # The hold the card was issued on; `bound_hold_id` moves on the one automatic re-hold.
    issued_hold_id: str | None = None

    @property
    def card(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.card_json)
        return data

    @property
    def material(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.material_json)
        return data

    @property
    def live(self) -> bool:
        return self.status in LIVE


def _row(row: Any) -> ApprovalRow:
    return ApprovalRow(*(row[i] for i in range(19)))


@dataclass(frozen=True)
class PendingResume:
    approval_id: str
    conversation_id: str
    inbound_json: str
    created_at: int
    started_run_id: str | None


class ApprovalsRepo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self, approval_id: str) -> ApprovalRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?", (approval_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def get_by_proposal(self, proposal_id: str) -> ApprovalRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE proposal_id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def live_for_conversation(self, conversation_id: str) -> ApprovalRow | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE conversation_id = ? "
            "AND status IN ('pending', 'granted')",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list_for_conversation(self, conversation_id: str) -> list[ApprovalRow]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE conversation_id = ? "
            "ORDER BY requested_at, rowid",
            (conversation_id,),
        ) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def insert(self, row: ApprovalRow) -> None:
        await self._conn.execute(
            f"INSERT INTO approvals ({_COLUMNS}) VALUES ({', '.join('?' * 19)})",
            (
                row.approval_id,
                row.conversation_id,
                row.errand_id,
                row.proposal_id,
                row.action,
                row.fingerprint,
                row.material_json,
                row.card_json,
                row.bound_hold_id,
                row.status,
                row.granted_via,
                row.granted_message_id,
                row.invalidated_reason,
                row.consumed_by_intent,
                row.requested_at,
                row.granted_at,
                row.consumed_at,
                row.expires_at,
                row.issued_hold_id,
            ),
        )

    async def _ids(self, sql: str, params: tuple[Any, ...]) -> list[str]:
        async with self._conn.execute(sql, params) as cur:
            return [str(r[0]) for r in await cur.fetchall()]

    async def expire_lapsed(
        self, conversation_id: str, now_ms: int, grace_ms: int
    ) -> list[tuple[str, str]]:
        """Pending rows past expires_at and granted rows past granted_at + grace.

        Returns (approval_id, status it left)."""
        pending = await self._ids(
            "UPDATE approvals SET status = 'expired', invalidated_reason = 'expired' "
            "WHERE conversation_id = ? AND status = 'pending' AND expires_at <= ? "
            "RETURNING approval_id",
            (conversation_id, now_ms),
        )
        granted = await self._ids(
            "UPDATE approvals SET status = 'expired', invalidated_reason = 'grant_not_executed' "
            "WHERE conversation_id = ? AND status = 'granted' AND consumed_by_intent IS NULL "
            "AND granted_at + ? <= ? RETURNING approval_id",
            (conversation_id, grace_ms, now_ms),
        )
        return [(a, "pending") for a in pending] + [(a, "granted") for a in granted]

    async def supersede_live(self, conversation_id: str, reason: str) -> list[str]:
        return await self._ids(
            "UPDATE approvals SET status = 'invalidated', invalidated_reason = ? "
            "WHERE conversation_id = ? AND status IN ('pending', 'granted') RETURNING approval_id",
            (reason, conversation_id),
        )

    async def grant(
        self,
        approval_id: str,
        *,
        fingerprint: str,
        via: Literal["button", "typed"],
        message_id: str | None,
        now_ms: int,
    ) -> bool:
        cur = await self._conn.execute(
            "UPDATE approvals SET status = 'granted', granted_via = ?, granted_message_id = ?, "
            "granted_at = ? WHERE approval_id = ? AND status = 'pending' AND fingerprint = ? "
            "AND expires_at > ?",
            (via, message_id, now_ms, approval_id, fingerprint, now_ms),
        )
        return cur.rowcount == 1

    async def decline(self, approval_id: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE approvals SET status = 'declined' WHERE approval_id = ? "
            "AND status IN ('pending', 'granted') AND consumed_by_intent IS NULL",
            (approval_id,),
        )
        return cur.rowcount == 1

    async def consume(
        self, approval_id: str, *, fingerprint: str, intent_id: str, now_ms: int, grace_ms: int
    ) -> int:
        cur = await self._conn.execute(
            "UPDATE approvals SET status = 'consumed', consumed_at = ?, consumed_by_intent = ? "
            "WHERE approval_id = ? AND status = 'granted' AND fingerprint = ? "
            "AND granted_at + ? > ?",
            (now_ms, intent_id, approval_id, fingerprint, grace_ms, now_ms),
        )
        return int(cur.rowcount)

    async def rebind_hold(self, approval_id: str, *, old_hold_id: str, new_hold_id: str) -> bool:
        """Move a granted approval to the hold that replaced a lapsed one (9.4 step 2).
        Conditional on the hold it leaves, so a repeat cannot move it twice."""
        cur = await self._conn.execute(
            "UPDATE approvals SET bound_hold_id = ? WHERE approval_id = ? "
            "AND status = 'granted' AND bound_hold_id = ?",
            (new_hold_id, approval_id, old_hold_id),
        )
        return cur.rowcount == 1

    async def invalidate(self, approval_id: str, reason: str) -> bool:
        cur = await self._conn.execute(
            "UPDATE approvals SET status = 'invalidated', invalidated_reason = ? "
            "WHERE approval_id = ? AND status IN ('pending', 'granted')",
            (reason, approval_id),
        )
        return cur.rowcount == 1

    # ---------------------------------------------------------- pending resumes

    async def insert_resume(
        self, approval_id: str, conversation_id: str, inbound: dict[str, Any], now_ms: int
    ) -> None:
        await self._conn.execute(
            "INSERT INTO pending_resumes (approval_id, conversation_id, inbound_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (approval_id, conversation_id, json.dumps(inbound, separators=(",", ":")), now_ms),
        )

    async def get_resume(self, approval_id: str) -> PendingResume | None:
        async with self._conn.execute(
            "SELECT approval_id, conversation_id, inbound_json, created_at, started_run_id "
            "FROM pending_resumes WHERE approval_id = ?",
            (approval_id,),
        ) as cur:
            row = await cur.fetchone()
        return PendingResume(*(row[i] for i in range(5))) if row else None

    async def mark_resume_started(self, approval_id: str, run_id: str) -> None:
        await self._conn.execute(
            "UPDATE pending_resumes SET started_run_id = ? WHERE approval_id = ?",
            (run_id, approval_id),
        )

    async def delete_resume(self, approval_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM pending_resumes WHERE approval_id = ?", (approval_id,)
        )
