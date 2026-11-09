"""Code templates for what a cancel or reschedule returns (architecture 15.2, N1).

Every word comes from records: the approval's card (provider, slot) and the booking id
`list_bookings` showed. Model text never reaches these.
"""

from __future__ import annotations

from errand.agent import cards
from errand.agent.deps import RunDeps
from errand.domain.messages import ActionCard, MessageBody, OutcomeNotice, SlotOption
from errand.domain.outcomes import Changed, ChangeFailed, RunOutcome

_FAILED_TEXT = {
    ("cancel", "booking_changed"): (
        "That booking is not what the card showed any more, so I didn't touch it. "
        "Nothing was cancelled."
    ),
    ("reschedule", "booking_changed"): (
        "That booking is not what the card showed any more, so I didn't touch it. "
        "Nothing was moved."
    ),
    ("cancel", "still_confirmed"): (
        "I couldn't cancel it: the provider didn't answer, and the booking is still active. "
        "Nothing was cancelled."
    ),
    ("reschedule", "still_confirmed"): (
        "I couldn't move it: the provider didn't answer, and the booking is still at its old "
        "time. Nothing was moved."
    ),
    ("reschedule", "provider_cancelled"): (
        "The provider cancelled that booking, so it can't be moved. Nothing was moved."
    ),
    ("cancel", "rejected"): "The provider refused the cancellation. Nothing was cancelled.",
    ("reschedule", "rejected"): "The provider refused the change. Nothing was moved.",
    ("reschedule", "slot_conflict"): ("That time was just taken, so nothing was moved."),
    ("cancel", "in_progress"): "A change to that booking is already in progress.",
    ("reschedule", "in_progress"): "A change to that booking is already in progress.",
}


async def render_changed(
    deps: RunDeps, result: Changed, *, after_restart: bool = False
) -> tuple[RunOutcome, list[MessageBody]]:
    async with deps.db.session() as repos:
        row = await repos.approvals.get(result.approval_id)
    assert row is not None
    card = ActionCard.model_validate_json(row.card_json)
    if result.action == "cancel":
        return RunOutcome.CANCELLED_VERIFIED, [
            cards.cancelled_card(
                card,
                result.booking_id,
                by_provider=result.by_provider,
                after_restart=after_restart,
            )
        ]
    return RunOutcome.RESCHEDULED_VERIFIED, [
        cards.rescheduled_card(card, result.booking_id, after_restart=after_restart)
    ]


def render_failed(result: ChangeFailed) -> tuple[RunOutcome, list[MessageBody]]:
    text = _FAILED_TEXT.get(
        (result.action, result.reason), "That change did not go through. Nothing was changed."
    )
    alternatives = [
        SlotOption(ref=a.ref, start=a.start, end=a.end, label=a.label) for a in result.alternatives
    ]
    if alternatives:
        text += " Here are the other free times that day."
    return RunOutcome.ACTION_FAILED, [
        OutcomeNotice(outcome=RunOutcome.ACTION_FAILED, text=text, alternatives=alternatives)
    ]
