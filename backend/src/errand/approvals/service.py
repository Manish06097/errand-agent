"""The approval service: every change to an approval row goes through here (8.2 to 8.5).

Transitions, each a conditional UPDATE inside one `BEGIN IMMEDIATE` transaction:

- `create`: insert a pending row and supersede every older live row (pending or
  granted) in the conversation, in the same transaction. A re-run with the same
  derived `proposal_id` finds its own row and changes nothing (`UNIQUE(proposal_id)`).
- `grant`: `pending -> granted` only if the fingerprint matches and the row has not
  expired, with the `pending_resumes` row in the same transaction.
- `decline`: `pending | granted -> declined` (never once consumed).
- `consume`: `granted -> consumed`, row count exactly 1, together with the caller's
  write-ahead intent; the caller's re-check runs first inside the same transaction.
- `invalidate`: `pending | granted -> invalidated` with a reason.
- the lazy sweep: `pending` past `expires_at` and `granted` past `granted_at + grace`
  become `expired`. It runs at the start of every transaction above and before every
  "is there a live approval" read, so an expired row never counts as live.

All times are epoch ms on the agent clock, which the caller passes in.

`ApprovalToken` is what the consequential client demands for `book`, `cancel` and
`reschedule`. It is minted only here, after a consume commits: the constructor checks
a private sentinel, every minted token is remembered, and copying or pickling one is
refused, so a token built any other way is rejected at the call (`is_minted`).
"""

from __future__ import annotations

import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, NoReturn

from errand.faults import fault_point
from errand.store.agent_db import AgentDB, Repos
from errand.store.repos.approvals import ApprovalRow

_MINT = object()
_minted: weakref.WeakSet[ApprovalToken] = weakref.WeakSet()


class ApprovalToken:
    """Proof, in memory, that `consume` committed for this approval and intent."""

    __slots__ = ("__weakref__", "approval_id", "fingerprint", "intent_id")

    approval_id: str
    intent_id: str
    fingerprint: str

    def __init__(self, mint: object, *, approval_id: str, intent_id: str, fingerprint: str):
        if mint is not _MINT:
            raise TypeError("an ApprovalToken is minted only by errand.approvals.service")
        object.__setattr__(self, "approval_id", approval_id)
        object.__setattr__(self, "intent_id", intent_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        _minted.add(self)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("an ApprovalToken is immutable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("an ApprovalToken cannot be copied or pickled")

    def __repr__(self) -> str:
        return f"ApprovalToken(approval_id={self.approval_id!r}, intent_id={self.intent_id!r})"


def is_minted(token: object) -> bool:
    return isinstance(token, ApprovalToken) and token in _minted


@dataclass(frozen=True)
class ApprovalPolicy:
    ttl_ms: int
    grace_ms: int


Swept = list[tuple[str, str]]  # (approval_id, the status it left)


def _in_txn(repos: Repos) -> None:
    if not repos.conn.in_transaction:
        raise RuntimeError("approval transitions run inside BEGIN IMMEDIATE")


async def sweep(repos: Repos, conversation_id: str, now_ms: int, policy: ApprovalPolicy) -> Swept:
    _in_txn(repos)
    return await repos.approvals.expire_lapsed(conversation_id, now_ms, policy.grace_ms)


async def live_approval(
    db: AgentDB, conversation_id: str, now_ms: int, policy: ApprovalPolicy
) -> tuple[ApprovalRow | None, Swept]:
    """The conversation's one live approval after the sweep, and what the sweep ended."""
    async with db.transaction() as repos:
        swept = await sweep(repos, conversation_id, now_ms, policy)
        row = await repos.approvals.live_for_conversation(conversation_id)
    return row, swept


@dataclass(frozen=True)
class Created:
    row: ApprovalRow
    created: bool
    superseded: list[str] = field(default_factory=list)
    swept: Swept = field(default_factory=list)


async def create(db: AgentDB, row: ApprovalRow, policy: ApprovalPolicy) -> Created:
    if row.status != "pending":
        raise ValueError("a new approval starts pending")
    async with db.transaction() as repos:
        existing = await repos.approvals.get_by_proposal(row.proposal_id)
        if existing is not None:
            # A re-run of prepare_proposal: the row is already there, maybe already
            # granted or superseded; either way nothing is inserted twice.
            return Created(existing, created=False)
        swept = await sweep(repos, row.conversation_id, row.requested_at, policy)
        superseded = await repos.approvals.supersede_live(row.conversation_id, "superseded")
        await repos.approvals.insert(row)
    return Created(row, created=True, superseded=superseded, swept=swept)


class GrantOutcome(StrEnum):
    GRANTED = "granted"
    ALREADY = "already"  # granted or consumed before: a repeat click
    EXPIRED = "expired"
    CHANGED = "changed"  # pending, but the fingerprint is not the card's
    GONE = "gone"  # declined or invalidated
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class GrantResult:
    outcome: GrantOutcome
    row: ApprovalRow | None
    swept: Swept = field(default_factory=list)


def _why_not(row: ApprovalRow) -> GrantOutcome:
    if row.status in ("granted", "consumed"):
        return GrantOutcome.ALREADY
    if row.status == "expired":
        return GrantOutcome.EXPIRED
    if row.status == "pending":
        return GrantOutcome.CHANGED
    return GrantOutcome.GONE


async def grant(
    db: AgentDB,
    *,
    approval_id: str,
    fingerprint: str,
    via: Literal["button", "typed"],
    message_id: str | None,
    resume_inbound: dict[str, Any],
    now_ms: int,
    policy: ApprovalPolicy,
) -> GrantResult:
    async with db.transaction() as repos:
        row = await repos.approvals.get(approval_id)
        if row is None:
            return GrantResult(GrantOutcome.NOT_FOUND, None)
        swept = await sweep(repos, row.conversation_id, now_ms, policy)
        ok = await repos.approvals.grant(
            approval_id, fingerprint=fingerprint, via=via, message_id=message_id, now_ms=now_ms
        )
        if ok:
            # Same transaction: a crash between the grant and the resume leaves the
            # row the reconciler resumes from (9.7), never a stranded grant.
            await repos.approvals.insert_resume(
                approval_id, row.conversation_id, resume_inbound, now_ms
            )
        after = await repos.approvals.get(approval_id)
    assert after is not None
    if ok:
        fault_point("after_grant_commit")
    return GrantResult(GrantOutcome.GRANTED if ok else _why_not(after), after, swept)


async def decline(
    db: AgentDB, *, approval_id: str, now_ms: int, policy: ApprovalPolicy
) -> GrantResult:
    async with db.transaction() as repos:
        row = await repos.approvals.get(approval_id)
        if row is None:
            return GrantResult(GrantOutcome.NOT_FOUND, None)
        swept = await sweep(repos, row.conversation_id, now_ms, policy)
        ok = await repos.approvals.decline(approval_id)
        if ok:
            await repos.approvals.delete_resume(approval_id)
        after = await repos.approvals.get(approval_id)
    assert after is not None
    if ok:
        return GrantResult(GrantOutcome.GRANTED, after, swept)
    return GrantResult(_why_not(after), after, swept)


@dataclass(frozen=True)
class ConsumeRefused:
    reason: str
    status: str | None
    invalidated: bool = False


Recheck = Callable[[Repos], Awaitable[str | None]]
Write = Callable[[Repos], Awaitable[None]]


async def consume(
    db: AgentDB,
    *,
    approval_id: str,
    fingerprint: str,
    intent_id: str,
    now_ms: int,
    policy: ApprovalPolicy,
    recheck: Recheck,
    write: Write,
) -> ApprovalToken | ConsumeRefused:
    """The guard transaction (8.4): re-check, consume (exactly one row), write ahead.

    `recheck` returns an invalidation reason or None; a reason invalidates the approval
    in this same transaction, so a granted row is never left behind. `write` inserts
    the intent and its first attempt. The token is minted only after the commit.
    """
    async with db.transaction() as repos:
        row = await repos.approvals.get(approval_id)
        if row is None:
            return ConsumeRefused("not_found", None)
        await sweep(repos, row.conversation_id, now_ms, policy)
        reason = await recheck(repos)
        if reason is not None:
            invalidated = await repos.approvals.invalidate(approval_id, reason)
            current = await repos.approvals.get(approval_id)
            return ConsumeRefused(reason, current.status if current else None, invalidated)
        n = await repos.approvals.consume(
            approval_id,
            fingerprint=fingerprint,
            intent_id=intent_id,
            now_ms=now_ms,
            grace_ms=policy.grace_ms,
        )
        if n != 1:
            current = await repos.approvals.get(approval_id)
            return ConsumeRefused("not_consumable", current.status if current else None)
        await write(repos)
    return ApprovalToken(
        _MINT, approval_id=approval_id, intent_id=intent_id, fingerprint=fingerprint
    )


async def expire_unexecuted_grants(db: AgentDB) -> list[ApprovalRow]:
    """Startup (9.7 as trimmed): every `granted` row no intent consumed ends `expired` with
    reason `grant_not_executed`, and its `pending_resumes` row is closed. A grant is never
    resumed after a restart: the user hears that nothing was booked and gets a fresh card.

    Returns the rows as they were before, for the recovery run that tells the user.
    """
    async with db.transaction() as repos:
        rows = await repos.approvals.unexecuted_grants()
        for row in rows:
            await repos.approvals.expire_unexecuted(row.approval_id)
            await repos.approvals.delete_resume(row.approval_id)
    return rows


async def invalidate(db: AgentDB, approval_id: str, reason: str) -> bool:
    async with db.transaction() as repos:
        return await repos.approvals.invalidate(approval_id, reason)
