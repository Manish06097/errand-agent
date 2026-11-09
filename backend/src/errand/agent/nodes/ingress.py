"""Ingress: every input lands here first (6.2).

It loads the thread's errand, starts one if there is none (or the last one finished),
resets the per-run fields, runs the approval sweep and routes:

- `UserMessage` -> plan (or straight to respond with the code hint, for a bare
  acknowledgement the messages route marked `confirm_hint`);
- `UiAction(select_slot | refresh_hold)` -> prepare_proposal, no model call;
- `ApprovalGranted` -> execute, which re-reads the row: the inbound is a hint;
- `ApprovalDeclined` -> respond (DECLINED);
- `Recover(intent_id)` and `UiAction(check_now)` -> verify, for an intent that is not
  settled yet (agent/recovery.py; N1). The intent and its attempt come from our own
  tables, so the route does not depend on where LangGraph says the thread stopped.

A thread can be parked on a card that the sweep has since expired, or that something
else ended. Ingress notices from the approvals table, not from state, moves the errand
off AWAITING_APPROVAL (to COMPARING) and routes the input normally; respond then does
not park on it again.

A re-run after a crash produces the same result: the new errand's id is derived from
the run id, and the only write is the sweep, which is idempotent.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.runtime import Runtime

from errand.agent import recovery
from errand.agent.deps import RunDeps
from errand.agent.records import live_approval
from errand.agent.state import ThreadState
from errand.agent.transitions import INITIAL_PHASE, transition
from errand.domain.brief import Brief, ErrandKind
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import (
    ApprovalDeclined,
    ApprovalGranted,
    Recover,
    SystemEvent,
    UiAction,
    UserMessage,
)
from errand.domain.outcomes import BookCalled, ConfirmHint, Declined, Failure
from errand.ids import errand_id_for

TERMINAL = frozenset({Phase.COMPLETED, Phase.FAILED, Phase.ABANDONED})
M4_ACTIONS = frozenset({"select_slot", "refresh_hold"})
FOLLOWUP_ACTIONS = frozenset({"widen_followup", "dismiss_followup"})
RECHECK_ACTIONS = frozenset({"check_now"})


async def ingress(state: ThreadState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
    deps = runtime.context
    errand = state.errand
    if errand is None or errand.phase in TERMINAL:
        errand = Errand(
            errand_id=errand_id_for(deps.run_id),
            kind=ErrandKind.BOOK,
            phase=INITIAL_PHASE,
            brief=Brief(),
        )
    deps.tracer.errand_id = errand.errand_id
    with deps.tracer.node("ingress", phase_before=errand.phase.value) as span:
        update: dict[str, Any] = {
            "plan_result": None,
            "result": None,
            "outcome": None,
            "park": None,
            "system_notes": [],
            "card_changes": [],
            "followup": None,
        }
        live = await live_approval(deps)
        inbound = state.inbound
        rechecks = isinstance(inbound, Recover) or (
            isinstance(inbound, UiAction) and inbound.kind in RECHECK_ACTIONS
        )
        # A re-check is about an intent, not a card: the approval it consumed is not live
        # by design, and the errand must not be moved off the phase verify starts from.
        if (
            not rechecks
            and errand.phase is Phase.AWAITING_APPROVAL
            and (live is None or live.approval_id != errand.approval_id)
        ):
            errand = await _off_dead_card(deps, errand)
        if isinstance(inbound, UserMessage):
            if inbound.confirm_hint and live is not None and live.status == "pending":
                update["result"] = ConfirmHint(approval_id=live.approval_id)
        elif isinstance(inbound, Recover) or (
            isinstance(inbound, UiAction) and inbound.kind in RECHECK_ACTIONS
        ):
            errand, update["result"] = await recovery.reopen(deps, errand, inbound)
        elif isinstance(inbound, UiAction):
            if inbound.payload.get("recovery") == "grant_not_executed":
                # The reconciler expired this grant before the run started (a restart);
                # the trace of the run that tells the user records it (INV-15).
                gone = inbound.payload.get("approval_id", "")
                deps.tracer.emit("approval.expired", approval_id=gone, reason="grant_not_executed")
                deps.tracer.emit(
                    "card.update", approval_id=gone, status="expired", reason="grant_not_executed"
                )
            if inbound.kind not in M4_ACTIONS | FOLLOWUP_ACTIONS:
                update["plan_result"] = Failure(reason=f"ui action {inbound.kind} is not built yet")
        elif isinstance(inbound, SystemEvent):
            pass  # followup_start and followup_lapsed: the followup node reads the job
        elif isinstance(inbound, ApprovalGranted):
            async with deps.db.session() as repos:
                row = await repos.approvals.get(inbound.approval_id)
            if row is not None and row.status == "granted":
                deps.tracer.emit(
                    "approval.granted",
                    approval_id=row.approval_id,
                    fingerprint=row.fingerprint,
                    via=row.granted_via,
                    message_id=row.granted_message_id,
                )
                deps.tracer.emit("card.update", approval_id=row.approval_id, status="granted")
        elif isinstance(inbound, ApprovalDeclined):
            async with deps.db.session() as repos:
                row = await repos.approvals.get(inbound.approval_id)
            if row is not None and row.status == "declined":
                deps.tracer.emit("approval.declined", approval_id=row.approval_id)
                deps.tracer.emit("card.update", approval_id=row.approval_id, status="declined")
            update["result"] = Declined(approval_id=inbound.approval_id)
        else:
            kind = inbound.type if inbound is not None else "none"
            update["plan_result"] = Failure(reason=f"inbound {kind} is not handled until M5")
        update["errand"] = errand
        span.end_fields["phase_after"] = errand.phase.value
    return update


async def _off_dead_card(deps: RunDeps, errand: Errand) -> Errand:
    status = "missing"
    if errand.approval_id is not None:
        async with deps.db.session() as repos:
            row = await repos.approvals.get(errand.approval_id)
        if row is not None:
            status = row.status
    reason = {
        "declined": "declined",
        "expired": "approval_expired",
        "invalidated": "approval_invalidated",
        "consumed": "approval_consumed",
    }.get(status, "approval_gone")
    return transition(errand, Phase.COMPARING, reason, deps.tracer)


Route = Literal["plan", "prepare_proposal", "execute", "verify", "respond", "followup"]


def route_after_ingress(state: ThreadState) -> Route:
    if isinstance(state.result, BookCalled):
        return "verify"
    if state.result is not None or state.plan_result is not None:
        return "respond"
    inbound = state.inbound
    if isinstance(inbound, UserMessage):
        return "plan"
    if isinstance(inbound, SystemEvent) or (
        isinstance(inbound, UiAction) and inbound.kind in FOLLOWUP_ACTIONS
    ):
        return "followup"
    if isinstance(inbound, UiAction) and inbound.kind in M4_ACTIONS:
        return "prepare_proposal"
    if isinstance(inbound, ApprovalGranted):
        return "execute"
    return "respond"
