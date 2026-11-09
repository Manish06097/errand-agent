"""What a brief correction does to the work built on the old brief (7.3, 8.5).

`brief_ops.INVALIDATES` and `brief_ops.invalidations` decide, as pure data and a pure
function, what a changed field discards. This module applies the result after
`update_brief` has merged a patch:

- every hit is traced as `invalidate {what, id, reason}`;
- a hold in the hit list is dropped from the errand (the world keeps the hold until it
  lapses; nothing here calls the world);
- a card in the hit list is invalidated through the approval service with the reason
  `request_changed`: a conditional UPDATE, so a click or a consume that committed first
  decides. If the row can no longer be invalidated (it is already consumed), the booking
  path stands, nothing is traced as invalidated, and the run's status line says what
  actually happened (`CardChange(outcome="kept")`);
- an invalidated card is traced twice, `approval.invalidated` and a `card.update` that
  carries `changed_fields`, and the errand leaves AWAITING_APPROVAL for COMPARING, or for
  GATHERING when the correction cleared a required field.

The shortlist is only marked stale (the brief version moved), never edited: the planner's
next `search_providers` rebuilds it (7.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from errand.agent import brief_ops
from errand.agent.deps import RunDeps
from errand.agent.records import live_approval
from errand.agent.state import CardChange
from errand.agent.transitions import transition
from errand.approvals import service
from errand.domain.brief import Brief, BriefField
from errand.domain.errand import Errand, Phase

REQUEST_CHANGED = "request_changed"


@dataclass
class Applied:
    errand: Errand
    hits: list[brief_ops.Invalidation] = field(default_factory=list)
    changes: list[CardChange] = field(default_factory=list)


def changed_words(fields: list[BriefField]) -> str:
    words = [brief_ops.CHANGED_WORDS[f] for f in fields if f in brief_ops.CHANGED_WORDS]
    return " and ".join(dict.fromkeys(words)) or "your request"


async def apply(
    deps: RunDeps,
    errand: Errand,
    old_brief: Brief,
    outcome: brief_ops.PatchOutcome,
) -> Applied:
    """`errand` still has the shortlist, hold and card of the old brief; its brief is the
    merged one. Returns the errand without what the change discarded."""
    changed = [*outcome.set_fields, *outcome.cleared_fields]
    hits: list[brief_ops.Invalidation] = []
    for name in dict.fromkeys(changed):
        hits += brief_ops.invalidations(name, old_brief, errand.brief, errand, deps.tz)
    result = Applied(errand=errand, hits=hits)
    for hit in hits:
        deps.tracer.emit(
            "invalidate",
            what=hit.what,
            fields={"id": hit.id, "changed": [hit.field.value]},
            reason=hit.reason,
        )
    discarded = {hit.what for hit in hits}
    kept = False
    if "approval" in discarded and errand.approval_id is not None:
        words = [f.value for f in dict.fromkeys(h.field for h in hits if h.what == "approval")]
        row = await live_approval(deps)
        if row is not None and row.approval_id == errand.approval_id:
            if await service.invalidate(deps.db, row.approval_id, REQUEST_CHANGED):
                _trace_invalidated(deps, row.approval_id, row.fingerprint, words)
                result.changes.append(
                    CardChange(
                        approval_id=row.approval_id, outcome="invalidated", changed_fields=words
                    )
                )
                result.errand = _off_the_card(deps, result.errand, outcome)
            else:
                # The conditional UPDATE lost: the booking path owns this card now.
                kept = True
                async with deps.db.session() as repos:
                    now = await repos.approvals.get(row.approval_id)
                result.changes.append(
                    CardChange(
                        approval_id=row.approval_id,
                        outcome="kept",
                        changed_fields=words,
                        status=now.status if now else None,
                    )
                )
    if ("hold" in discarded or "approval" in discarded) and not kept:
        result.errand = result.errand.model_copy(update={"hold": None})
    return result


def _trace_invalidated(deps: RunDeps, approval_id: str, fingerprint: str, words: list[str]) -> None:
    deps.tracer.emit(
        "approval.invalidated", approval_id=approval_id, reason=REQUEST_CHANGED, old_fp=fingerprint
    )
    deps.tracer.emit(
        "card.update",
        approval_id=approval_id,
        status="invalidated",
        reason=REQUEST_CHANGED,
        changed_fields=words,
    )


def _off_the_card(deps: RunDeps, errand: Errand, outcome: brief_ops.PatchOutcome) -> Errand:
    """AWAITING_APPROVAL -> COMPARING, or -> GATHERING when a required field was cleared."""
    errand = errand.model_copy(update={"approval_id": None})
    if errand.phase is not Phase.AWAITING_APPROVAL:
        return errand
    required = set(brief_ops.REQUIRED[errand.kind])
    to = Phase.GATHERING if required & set(outcome.cleared_fields) else Phase.COMPARING
    return transition(errand, to, "approval_invalidated", deps.tracer)
