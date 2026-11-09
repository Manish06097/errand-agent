"""Write-ahead intents and attempts for a cancel or reschedule (architecture 9.2, 9.3, N1).

Like `execution/intents.py` for a booking, with no hold: the intent's `target_booking_id`
is the booking, and the first attempt carries the key from `derive_idem_key`:

- cancel: subject = booking_id
- reschedule: subject = f"{booking_id}|{new_slot_start_iso}"

Ids are derived from the approval, never minted, so a re-run finds its own rows.
"""

from __future__ import annotations

from errand.execution.idempotency import derive_idem_key
from errand.execution.intents import attempt_id_for, intent_id_for
from errand.store.repos.intents import AttemptRow, IntentRow


def change_intent(
    *,
    action: str,
    approval_id: str,
    conversation_id: str,
    errand_id: str,
    user_id: str,
    fingerprint: str,
    material_json: str,
    booking_id: str,
    now_ms: int,
) -> IntentRow:
    return IntentRow(
        intent_id=intent_id_for(approval_id),
        conversation_id=conversation_id,
        errand_id=errand_id,
        user_id=user_id,
        action=action,
        approval_id=approval_id,
        fingerprint=fingerprint,
        material_json=material_json,
        target_booking_id=booking_id,
        replaces_booking_id=None,
        same_key_retries=0,
        state="open",
        result_json=None,
        created_at=now_ms,
        updated_at=now_ms,
    )


def first_change_attempt(
    intent: IntentRow, *, subject: str, fence_epoch: int, now_ms: int
) -> AttemptRow:
    """Attempt 1, `prepared`: provably unsent until it is moved to `in_flight`."""
    return AttemptRow(
        attempt_id=attempt_id_for(intent.intent_id, 1),
        intent_id=intent.intent_id,
        n=1,
        hold_id=None,
        hold_expires_at=None,
        idempotency_key=derive_idem_key(intent.action, intent.approval_id, subject),
        state="prepared",
        calls=0,
        status_checks=0,
        last_error=None,
        fence_epoch=fence_epoch,
        created_at=now_ms,
        updated_at=now_ms,
    )
