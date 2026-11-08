"""Idempotency keys for consequential calls (architecture 9.3).

The key is derived from the approval and the subject the provider sees, persisted on
the attempt row before the call (write-ahead) and always read back from that row. The
derivation is a second guard: the same approval and hold give the same key even if a
row were lost.

- book: subject = hold_id (a new hold is a new request to the provider)
- cancel: subject = booking_id
- reschedule: subject = f"{booking_id}|{new_slot_start_iso}"
"""

from __future__ import annotations

import hashlib


def derive_idem_key(action: str, approval_id: str, subject: str) -> str:
    h = hashlib.sha256(f"errand/v1|{action}|{approval_id}|{subject}".encode()).hexdigest()[:32]
    return f"{action[:2]}_{h}"
