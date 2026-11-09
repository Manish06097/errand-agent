"""The critical section's one exit (architecture 9.4, 15.1).

Once an intent exists, `execute -> verify -> respond` runs to completion: Stop, the
tool-call cap, the cost cap and the run's deadline do not apply inside it. So any exit
that finds an intent still `open` for the run ends it as PENDING_VERIFICATION with the
intent `needs_attention`, never LIMIT_REACHED, WALL_CLOCK_EXCEEDED or INTERNAL_ERROR: the
provider may already have acted, and only the reconciler's status check says whether it
did.

`exit_critical_section` is that one function. Every exit path that can end a run with an
intent open calls it:

- `respond`, which would otherwise render a limit, a tool outage or an error (N1);
- `RunManager`'s outer handler, for an exception in any node or the 90 s backstop (N1);
- N3 wires the wall-clock deadline check and the node exception wrapper into it too, so
  that there is exactly one place that decides "an open intent turns every exit into
  PENDING_VERIFICATION".

It reads and writes only our own tables. It calls neither the world nor the model, so it
cannot itself fail for the reasons the exit it handles failed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from datetime import tzinfo
from typing import Final

from errand.agent.templates import clock_hhmm, pending_text
from errand.domain.messages import NoticeAction, OutcomeNotice, PendingFacts
from errand.domain.outcomes import PendingCheck, RunOutcome
from errand.store.agent_db import AgentDB
from errand.trace.tracer import Tracer

log = logging.getLogger(__name__)

EXIT_REASONS: Final = ("limit", "exception", "backstop", "deadline")


async def exit_critical_section(
    db: AgentDB,
    *,
    conversation_id: str,
    intent_ids: Collection[str],
    now_ms: int,
    tracer: Tracer | None = None,
    reason: str,
) -> PendingCheck | None:
    """Move this run's `open` intents (`intent_ids`: what the run wrote or took over,
    `RunDeps.intents`) to `needs_attention`; the PENDING_VERIFICATION result for the
    latest one, or None when none was open.

    A run leaves its intent `open` only between the guard transaction and verify's
    decision. The attempt keeps its state (`prepared`, `in_flight` or `unknown`): the
    reconciler re-checks all three through the world.
    """
    moved: list[tuple[str, str, str]] = []
    async with db.transaction() as repos:
        for intent in await repos.intents.unresolved_for_conversation(conversation_id):
            if intent.state != "open" or intent.intent_id not in intent_ids:
                continue
            ok = await repos.intents.resolve_intent(
                intent.intent_id,
                frm=("open",),
                to="needs_attention",
                result_json=json.dumps({"reason": "critical_section_exit", "detail": reason}),
                now_ms=now_ms,
            )
            if ok:
                moved.append((intent.intent_id, intent.approval_id, reason))
    if not moved:
        return None
    for intent_id, _, why in moved:
        log.warning("intent %s left open by a %s exit; now needs_attention", intent_id, why)
        if tracer is not None:
            try:
                tracer.emit(
                    "intent.resolved",
                    intent_id=intent_id,
                    state="needs_attention",
                    error=f"critical_section_exit:{why}",
                )
            except Exception:
                log.exception("could not trace the critical-section exit of %s", intent_id)
    intent_id, approval_id, _ = moved[-1]
    return PendingCheck(
        approval_id=approval_id,
        intent_id=intent_id,
        reason="status_not_confirmed",
        detail=reason,
    )


def pending_notice(result: PendingCheck, tz: tzinfo) -> OutcomeNotice:
    """The PENDING_VERIFICATION message for a result, shared by `respond` and the
    RunManager's outer handler so both say the same thing. The facts and the wording are
    M5's (`templates.pending_text`, `PendingFacts`); "Check now" is offered whenever the
    provider's answer can still change; a mismatch the provider recorded offers to cancel
    that booking instead."""
    reason = "status_unavailable" if result.reason == "status_not_confirmed" else result.reason
    actions: list[NoticeAction] = []
    if reason in ("hold_live", "status_unavailable"):
        actions.append(NoticeAction(kind="check_now", label="Check now"))
    if reason == "material_mismatch" and result.booking_id:
        # The user's own quick reply (the client sends the label as their message); the
        # planner then proposes the cancel through the ordinary path (S65).
        actions.append(NoticeAction(kind="cancel", label=f"Cancel {result.booking_id}"))
    return OutcomeNotice(
        outcome=RunOutcome.PENDING_VERIFICATION,
        text=pending_text(result, tz),
        actions=actions,
        pending=PendingFacts(
            reason=reason,
            intent_id=result.intent_id,
            lapse_at=result.lapse_at,
            lapse_label=clock_hhmm(result.lapse_at, tz) if result.lapse_at else None,
            booking_id=result.booking_id,
            recorded=result.detail or None,
        ),
    )
