# ADR 0014: Proactive follow-up through a list_bookings watcher

## Context

When a booked provider cancels (a simulated event), the agent must detect it without a user prompt, decide whether and how to notify, find a replacement within known preferences, and get fresh approval before rebooking. The fixed contracts have no push channel.

## Decision

- A lifespan watcher polls `list_bookings` for users with upcoming bookings every 15 s (evals call `tick()`), diffs against `known_bookings`, and inserts a unique `followup_jobs` row per (booking, event) so each event is handled once across restarts.
- Job rows move `detected -> notified | ignored -> done`; the reconciler resumes a job left in `detected` after a crash.
- In the same transaction as the job insert, the watcher invalidates any pending or granted cancel or reschedule card for that booking.
- A code policy decides: ignore (past or already replaced), "nothing to cancel" (the user had a cancel card for it), urgent (within 24 h) or normal; no-fit notices offer widen buttons.
- A rebook intent carries `replaces_booking_id` under a unique index, so a replacement requested from the origin chat and one from the follow-up thread cannot both be booked.
- The follow-up runs in its own conversation linked from the origin one. Code clones the original brief, re-applies current memory, searches, filters, ranks and holds the top candidate, and shows a "Rebook" card. No model call until the user replies.

## Alternatives considered

- **World webhook**: outside the fixed contracts.
- **Check when the user next speaks**: not proactive.
- **Planner run for the follow-up**: more flexible wording, but the decision is policy, and a code path is deterministic and free.
- **Follow-up in the origin conversation**: collides with any errand open there.

## Consequences

- Polling adds `list_bookings` calls (cheap in the mock).
- The original approval is long consumed, so a rebook structurally needs a fresh one.
