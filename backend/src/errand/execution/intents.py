"""Write-ahead intents and attempts for one consumed approval (architecture 9.2).

Ids are derived from the approval, never minted, so a re-run of `execute` computes the
same intent id and finds its own row instead of making a second one.
"""

from __future__ import annotations

from errand.execution.idempotency import derive_idem_key
from errand.store.repos.intents import AttemptRow, IntentRow


def intent_id_for(approval_id: str) -> str:
    return f"in_{approval_id}"


def attempt_id_for(intent_id: str, n: int) -> str:
    return f"{intent_id}.a{n}"


def book_intent(
    *,
    approval_id: str,
    conversation_id: str,
    errand_id: str,
    user_id: str,
    fingerprint: str,
    material_json: str,
    now_ms: int,
) -> IntentRow:
    return IntentRow(
        intent_id=intent_id_for(approval_id),
        conversation_id=conversation_id,
        errand_id=errand_id,
        user_id=user_id,
        action="book",
        approval_id=approval_id,
        fingerprint=fingerprint,
        material_json=material_json,
        target_booking_id=None,
        replaces_booking_id=None,
        same_key_retries=0,
        state="open",
        result_json=None,
        created_at=now_ms,
        updated_at=now_ms,
    )


def first_book_attempt(
    intent: IntentRow, *, hold_id: str, hold_expires_at: int, fence_epoch: int, now_ms: int
) -> AttemptRow:
    """Attempt 1, `prepared`: provably unsent until it is moved to `in_flight`."""
    return book_attempt(
        intent,
        n=1,
        hold_id=hold_id,
        hold_expires_at=hold_expires_at,
        fence_epoch=fence_epoch,
        now_ms=now_ms,
    )


def book_attempt(
    intent: IntentRow,
    *,
    n: int,
    hold_id: str,
    hold_expires_at: int,
    fence_epoch: int,
    now_ms: int,
) -> AttemptRow:
    """Attempt `n`, `prepared`. A new hold is a new request to the provider, so a later
    attempt (only after every earlier one is `not_applied`, 9.2) gets a key derived from
    its own hold."""
    return AttemptRow(
        attempt_id=attempt_id_for(intent.intent_id, n),
        intent_id=intent.intent_id,
        n=n,
        hold_id=hold_id,
        hold_expires_at=hold_expires_at,
        idempotency_key=derive_idem_key("book", intent.approval_id, hold_id),
        state="prepared",
        calls=0,
        status_checks=0,
        last_error=None,
        fence_epoch=fence_epoch,
        created_at=now_ms,
        updated_at=now_ms,
    )
