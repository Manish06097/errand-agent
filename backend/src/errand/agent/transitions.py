"""The only way an errand's phase changes (architecture 7.2).

`transition()` checks the pair against `ALLOWED` and emits `state.transition`. An
illegal pair is still recorded, with the reason prefixed `rejected:`, and then raises
`IllegalTransition`, which ends the run with INTERNAL_ERROR. Recording the attempt
means INV-08, which re-checks every trace against this same table, sees it too.
"""

from __future__ import annotations

from collections.abc import Mapping

from errand.domain.errand import Errand, Phase
from errand.trace.tracer import Tracer

P = Phase

ALLOWED: Mapping[Phase, frozenset[Phase]] = {
    P.GATHERING: frozenset({P.GATHERING, P.COMPARING, P.QUOTING, P.ABANDONED}),
    P.COMPARING: frozenset({P.COMPARING, P.GATHERING, P.QUOTING, P.ABANDONED}),
    P.QUOTING: frozenset({P.AWAITING_APPROVAL, P.COMPARING}),
    P.AWAITING_APPROVAL: frozenset({P.EXECUTING, P.COMPARING, P.GATHERING, P.QUOTING, P.ABANDONED}),
    P.EXECUTING: frozenset({P.VERIFYING, P.EXECUTING, P.AWAITING_APPROVAL, P.COMPARING}),
    P.VERIFYING: frozenset({P.COMPLETED, P.EXECUTING, P.FAILED, P.NEEDS_ATTENTION, P.COMPARING}),
    P.NEEDS_ATTENTION: frozenset({P.VERIFYING, P.COMPLETED, P.FAILED, P.ABANDONED}),
    P.COMPLETED: frozenset(),
    P.FAILED: frozenset(),
    P.ABANDONED: frozenset(),
}

# A new errand starts here; creation is not a transition.
INITIAL_PHASE = P.GATHERING


class IllegalTransition(RuntimeError):
    def __init__(self, errand_id: str, frm: Phase, to: Phase, reason: str) -> None:
        super().__init__(f"errand {errand_id}: {frm.value} -> {to.value} is not allowed ({reason})")
        self.frm = frm
        self.to = to


def is_allowed(frm: Phase | str, to: Phase | str) -> bool:
    return Phase(to) in ALLOWED[Phase(frm)]


def transition(errand: Errand, to: Phase, reason: str, tracer: Tracer) -> Errand:
    """Return a copy of `errand` in phase `to`; never mutates the input."""
    frm = errand.phase
    if not is_allowed(frm, to):
        tracer.emit(
            "state.transition", **{"from": frm.value}, to=to.value, reason=f"rejected: {reason}"
        )
        raise IllegalTransition(errand.errand_id, frm, to, reason)
    tracer.emit("state.transition", **{"from": frm.value}, to=to.value, reason=reason)
    return errand.model_copy(update={"phase": to})
