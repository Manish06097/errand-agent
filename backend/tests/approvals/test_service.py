"""The approval service's transitions against a real agent.sqlite.

Every transition is a conditional UPDATE inside BEGIN IMMEDIATE; these tests pin the
conditions: the lazy sweep (pending past expiry, granted past the grace), the grant's
fingerprint and expiry checks with its pending_resumes row in the same transaction,
supersede and decline covering granted rows, the consume that must hit exactly one row,
and the ApprovalToken that only a committed consume can mint.
"""

from __future__ import annotations

import copy
import pickle
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from errand.approvals import service
from errand.approvals.service import ApprovalPolicy, ApprovalToken, GrantOutcome, is_minted
from errand.execution.executor import ConsequentialClient, TokenRefused
from errand.store.agent_db import AgentDB, Repos
from errand.store.repos.approvals import ApprovalRow

POLICY = ApprovalPolicy(ttl_ms=1_800_000, grace_ms=120_000)
T0 = 1_762_650_000_000


def row(approval_id: str, *, proposal: str | None = None, fp: str = "fp1_a") -> ApprovalRow:
    return ApprovalRow(
        approval_id=approval_id,
        conversation_id="c_1",
        errand_id="e_1",
        proposal_id=proposal or f"pr_{approval_id}",
        action="book",
        fingerprint=fp,
        material_json="{}",
        card_json="{}",
        bound_hold_id="H-1",
        status="pending",
        granted_via=None,
        granted_message_id=None,
        invalidated_reason=None,
        consumed_by_intent=None,
        requested_at=T0,
        granted_at=None,
        consumed_at=None,
        expires_at=T0 + POLICY.ttl_ms,
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AgentDB]:
    agent_db = await AgentDB.open(tmp_path / "agent.sqlite")
    yield agent_db
    await agent_db.close()


async def status(db: AgentDB, approval_id: str) -> str:
    async with db.session() as repos:
        found = await repos.approvals.get(approval_id)
    assert found is not None
    return found.status


async def grant(
    db: AgentDB, approval_id: str, *, fp: str = "fp1_a", at: int = T0 + 1
) -> GrantOutcome:
    result = await service.grant(
        db,
        approval_id=approval_id,
        fingerprint=fp,
        via="button",
        message_id=None,
        resume_inbound={"type": "approval_granted", "approval_id": approval_id, "via": "button"},
        now_ms=at,
        policy=POLICY,
    )
    return result.outcome


async def test_the_sweep_expires_pending_past_ttl_and_granted_past_the_grace(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    live, swept = await service.live_approval(db, "c_1", T0 + POLICY.ttl_ms - 1, POLICY)
    assert live is not None and swept == []
    live, swept = await service.live_approval(db, "c_1", T0 + POLICY.ttl_ms, POLICY)
    assert live is None and swept == [("ap_1", "pending")]
    assert await status(db, "ap_1") == "expired"

    await service.create(db, row("ap_2"), POLICY)
    assert await grant(db, "ap_2", at=T0 + 10) is GrantOutcome.GRANTED
    live, _ = await service.live_approval(db, "c_1", T0 + 10 + POLICY.grace_ms - 1, POLICY)
    assert live is not None and live.status == "granted"
    live, swept = await service.live_approval(db, "c_1", T0 + 10 + POLICY.grace_ms, POLICY)
    assert live is None and swept == [("ap_2", "granted")]


async def test_a_grant_writes_its_pending_resume_in_the_same_transaction(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    assert await grant(db, "ap_1") is GrantOutcome.GRANTED
    async with db.session() as repos:
        resume = await repos.approvals.get_resume("ap_1")
    assert resume is not None and resume.conversation_id == "c_1"
    assert '"approval_id":"ap_1"' in resume.inbound_json and resume.started_run_id is None


async def test_grant_answers_say_exactly_what_went_wrong(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    assert await grant(db, "ap_1", fp="fp1_other") is GrantOutcome.CHANGED
    assert await status(db, "ap_1") == "pending"
    assert await grant(db, "ap_1", at=T0 + POLICY.ttl_ms) is GrantOutcome.EXPIRED
    await service.create(db, row("ap_2"), POLICY)
    assert await grant(db, "ap_2") is GrantOutcome.GRANTED
    assert await grant(db, "ap_2") is GrantOutcome.ALREADY
    await service.create(db, row("ap_3"), POLICY)  # supersedes the granted ap_2
    assert await grant(db, "ap_2") is GrantOutcome.GONE
    assert await grant(db, "ap_missing") is GrantOutcome.NOT_FOUND


async def test_a_new_proposal_supersedes_granted_as_well_as_pending(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    assert await grant(db, "ap_1") is GrantOutcome.GRANTED
    created = await service.create(db, row("ap_2"), POLICY)
    assert created.created and created.superseded == ["ap_1"]
    assert await status(db, "ap_1") == "invalidated"


async def test_create_is_idempotent_on_the_proposal_id(db: AgentDB) -> None:
    first = await service.create(db, row("ap_1", proposal="pr_x"), POLICY)
    again = await service.create(db, row("ap_1", proposal="pr_x"), POLICY)
    assert first.created and not again.created and again.row.approval_id == "ap_1"
    async with db.session() as repos:
        assert len(await repos.approvals.list_for_conversation("c_1")) == 1


async def test_decline_ends_a_granted_row_and_drops_its_resume(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    await grant(db, "ap_1")
    result = await service.decline(db, approval_id="ap_1", now_ms=T0 + 2, policy=POLICY)
    assert result.outcome is GrantOutcome.GRANTED  # the decline went through
    assert await status(db, "ap_1") == "declined"
    async with db.session() as repos:
        assert await repos.approvals.get_resume("ap_1") is None


async def _nothing(_: Repos) -> str | None:
    return None


async def consume(db: AgentDB, written: list[str]) -> ApprovalToken | service.ConsumeRefused:
    async def write(repos: Repos) -> None:
        written.append("intent")

    return await service.consume(
        db,
        approval_id="ap_1",
        fingerprint="fp1_a",
        intent_id="in_ap_1",
        now_ms=T0 + 5,
        policy=POLICY,
        recheck=_nothing,
        write=write,
    )


async def test_consume_takes_exactly_one_granted_row_and_only_then_mints(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    written: list[str] = []
    refused = await consume(db, written)
    assert isinstance(refused, service.ConsumeRefused) and refused.status == "pending"
    assert written == []
    await grant(db, "ap_1", at=T0 + 1)
    token = await consume(db, written)
    assert isinstance(token, ApprovalToken) and is_minted(token) and written == ["intent"]
    again = await consume(db, written)
    assert isinstance(again, service.ConsumeRefused) and again.status == "consumed"
    assert written == ["intent"]


async def test_a_recheck_reason_invalidates_the_granted_row_in_the_same_transaction(
    db: AgentDB,
) -> None:
    await service.create(db, row("ap_1"), POLICY)
    await grant(db, "ap_1")

    async def duplicate(_: Repos) -> str | None:
        return "duplicate_risk"

    async def never(_: Repos) -> None:
        raise AssertionError("nothing is written after a failed re-check")

    refused = await service.consume(
        db,
        approval_id="ap_1",
        fingerprint="fp1_a",
        intent_id="in_ap_1",
        now_ms=T0 + 5,
        policy=POLICY,
        recheck=duplicate,
        write=never,
    )
    assert isinstance(refused, service.ConsumeRefused) and refused.invalidated
    assert await status(db, "ap_1") == "invalidated"


def test_an_approval_token_cannot_be_built_outside_the_service() -> None:
    with pytest.raises(TypeError, match=r"minted only by errand\.approvals\.service"):
        ApprovalToken(object(), approval_id="ap_1", intent_id="in_1", fingerprint="fp1_a")


async def test_a_minted_token_cannot_be_copied_and_a_forged_one_is_refused(db: AgentDB) -> None:
    await service.create(db, row("ap_1"), POLICY)
    await grant(db, "ap_1")
    token = await consume(db, [])
    assert isinstance(token, ApprovalToken)
    with pytest.raises(TypeError):
        copy.copy(token)
    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(AttributeError):
        token.intent_id = "in_other"
    forged = object.__new__(ApprovalToken)
    assert not is_minted(forged)
    client = ConsequentialClient(gateway=None)  # type: ignore[arg-type]
    with pytest.raises(TokenRefused):
        await client.book(forged, intent_id="in_ap_1", hold_id="H-1", idempotency_key="k")
    with pytest.raises(TokenRefused, match="not in_other"):
        await client.book(token, intent_id="in_other", hold_id="H-1", idempotency_key="k")
