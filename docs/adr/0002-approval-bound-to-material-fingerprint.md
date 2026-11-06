# ADR 0002: Approval bound to a material fingerprint, enforced in code

## Context

Booking, cancelling and rescheduling need explicit user confirmation of the exact action and material details, enforced by application code, not inferred from model output or tool arguments. Any material change to provider, slot or price must invalidate a prior approval.

## Decision

- An `approvals` row per proposal holds the card shown to the user, the canonical material details and `fingerprint = sha256(canonical_json(material))` with a schema version. Material is provider, service, slot, **held** price and any nearby same-service bookings for book; booking, its status, provider and slot for cancel; booking, its status, provider, old and new slot for reschedule. `hold_id` is stored but not material.
- The UI renders the card only from this row.
- **Grant paths**, both in the API under the thread lock: the button POST carrying the rendered card's fingerprint, or a typed message that (a) uses an action verb for the card's action ("book it", "cancel it", "confirm"; never a bare "yes" or "ok"), (b) carries `visible_card` with the card's `message_id`, captured when the user pressed Send, (c) is replying to the latest assistant message, which is that card with `typed_grant_ok`, and (d) was composed after the card was rendered. Both are a conditional `UPDATE ... WHERE status='pending' AND fingerprint=? AND expires_at > now`, written in the same transaction as a `pending_resumes` row.
- **Consumption** in `execute`, under a per-user consequential lock: recompute the fingerprint from current facts, re-run the duplicate checks, then in one transaction consume the approval (`status='granted' -> 'consumed'` within `granted_at + 120 s`, row count must be 1) and insert the write-ahead intent. Only then is an `ApprovalToken` minted, and the consequential client requires it.
- **Invalidation** on a new proposal, a brief change that touches the proposal, a re-hold that changes the fingerprint, expiry, material mismatch at execute time, the target booking no longer being active, another intent on the same target succeeding, or abandoning the errand. Every one of these covers `granted` rows as well as `pending` ones.
- One live approval per conversation (partial unique index over `pending` and `granted`). A `granted` row always reaches `consumed`, `invalidated` or `expired`; the reconciler resumes or expires any left behind by a crash.
- Over-budget quotes are not approvable.
- The planner has no tool that grants, consumes or reads approvals. Consequential tools are absent from its schema and rejected by `ToolGateway` if called.

## Alternatives considered

- **Model-classified "yes"** (Opus or Haiku): approval would be inferred from model output, which the brief forbids; injectable and nondeterministic.
- **`approved: true` in graph state, or a `confirm_booking` tool**: the model's own output would authorise the action.
- **Fingerprint bound to `hold_id`**: an unchanged re-hold after expiry would demand a second card with no safety gain.
- **Button-only approval**: safest, but typing a confirmation is natural in chat; position binding, compose-time capture and an action-verb grammar keep it deterministic.
- **Bare "yes" or "ok" as a grant while a card is visible**: design review broke it. A user answering the assistant's own side question ("want me to look at Saturday slots?") would have granted the cancel card underneath.
- **Approve on the search price**: the hold quote can differ, so the card always shows the quote.

## Consequences

- The resume payload is a hint; `execute` re-reads the row, so a forged payload carries no authority.
- Stale cards return 409 and refetch. The UI has to handle that path well.
- INV-01 and INV-12 check this in every scenario, including the hostile-model ones.
