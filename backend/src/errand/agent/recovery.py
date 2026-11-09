"""Re-entering verification for an intent that is not settled (architecture 9.7, N1).

Three things start a verification of an existing intent, and all three come through
`ingress` as an inbound that carries no authority of its own:

- `Recover(intent_id)`, from the reconciler (a restart, or its periodic re-check);
- `UiAction(check_now)`, from the "Check now" button on a PENDING_VERIFICATION message.

`reopen` turns either into what `verify` takes, a `BookCalled` for the intent's latest
attempt, after reading the intent and attempt from our own tables (never LangGraph's
`next`). It takes the per-user consequential lock the way `execute` does, so a
same-key re-send inside verify is serialised with everything else that acts for the user,
and `verify` releases it. Nothing here calls the world.
"""

from __future__ import annotations

from errand.agent.deps import RunDeps
from errand.agent.transitions import transition
from errand.domain.errand import Errand, Phase
from errand.domain.inbound import Recover, UiAction
from errand.domain.outcomes import AlreadyHandled, BookCalled, StepResult
from errand.store.repos.intents import AttemptRow, IntentRow

LIVE_ATTEMPT = ("prepared", "in_flight", "unknown")
# A check the agent could not finish (the booking was seen, the list was not) leaves the
# attempt `applied` and the intent `needs_attention`; verify asks again.
INCOMPLETE_CHECK = "check_incomplete"
NOTHING_TO_CHECK = "nothing_to_check"


async def unsettled_intent(deps: RunDeps, inbound: Recover | UiAction) -> IntentRow | None:
    """The intent this inbound is about, if it is still unresolved."""
    async with deps.db.session() as repos:
        if isinstance(inbound, Recover):
            intent = await repos.intents.get(inbound.intent_id)
            if intent is not None and intent.state in ("open", "needs_attention"):
                return intent
            return None
        unresolved = await repos.intents.unresolved_for_conversation(deps.conversation_id)
    return unresolved[-1] if unresolved else None


async def reopen(
    deps: RunDeps, errand: Errand, inbound: Recover | UiAction
) -> tuple[Errand, StepResult]:
    intent = await unsettled_intent(deps, inbound)
    attempt: AttemptRow | None = None
    if intent is not None:
        async with deps.db.session() as repos:
            attempts = await repos.intents.attempts(intent.intent_id)
        attempt = attempts[-1] if attempts else None
    incomplete = (
        intent is not None
        and intent.state == "needs_attention"
        and INCOMPLETE_CHECK in (intent.result_json or "")
    )
    if intent is None or attempt is None or not (attempt.state in LIVE_ATTEMPT or incomplete):
        # Settled between the request and the run (a tick got there first), or an
        # intent with nothing left to ask the provider about.
        approval_id = intent.approval_id if intent is not None else ""
        return errand, AlreadyHandled(approval_id=approval_id, reason=NOTHING_TO_CHECK)
    assert deps.consequential is not None, "the RunManager supplies the user locks"
    deps.intents.add(intent.intent_id)
    deps.held.append(await deps.consequential.acquire(deps.user_id))
    if errand.phase is Phase.AWAITING_APPROVAL:
        # The run that consumed the approval died before its phase was checkpointed.
        errand = transition(errand, Phase.EXECUTING, "recovery", deps.tracer)
    return errand, BookCalled(
        approval_id=intent.approval_id,
        intent_id=intent.intent_id,
        attempt_id=attempt.attempt_id,
        answer="unknown",
    )
