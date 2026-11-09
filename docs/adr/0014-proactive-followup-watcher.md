# ADR 0014: Proactive follow-up through a list_bookings watcher

## Context

When a booked provider cancels (a simulated event), the agent must detect it without a user prompt, decide whether and how to notify, find a replacement within known preferences, and get fresh approval before rebooking. The fixed contracts have no push channel.

## Decision

- A lifespan watcher polls `list_bookings` for every user with a conversation every 15 s (evals call `tick()`), mirrors every upcoming booking (not only the ones the agent made) into `known_bookings`, diffs, and inserts a unique `followup_jobs` row per (booking, event) so each event is handled once across restarts.
- Job rows move `detected -> notified | ignored`, `notified -> done` (`replaced`, `dismissed`, `lapsed`), or `detected -> deferred -> detected -> notified` when a saved `contact_window` is closed (delivery's gate defers a normal notice to the window's next opening, and an urgent one goes out at once with `quiet`, the badge only). A deferred job is woken back to `detected` when `deliver_at` has passed or the user does anything (any `/api` request, the event stream included); the reconciler resumes `detected` jobs and `deferred` ones once `deliver_at` has passed. The gate has no provider read, so a same-service notice within 24 h counts as urgent and goes out quietly.
- Delivery is a graph run on the follow-up conversation (`SystemEvent(followup_start)`), so the Rebook card uses the same grant, execute and verify path as any booking.
- In the same transaction as the job insert, the watcher invalidates any pending or granted cancel or reschedule card for that booking.
- A pure code policy decides: ignore (started or already replaced), notify without a search (a same-service booking already in the window), "nothing to cancel" (the user had a cancel card for it), reschedule window, urgent (within 24 h) or normal; no-fit notices offer three widen buttons.
- A rebook intent carries `replaces_booking_id` under a unique index, so a replacement requested from the origin chat and one from the follow-up thread cannot both be booked.
- The follow-up runs in its own conversation linked from the origin one by a `proactive` line. Code clones the original brief (or builds one from the booking), re-applies current memory, searches, filters, ranks and holds the top candidate under the same price ceiling, and shows a "Rebook" card. No model call and no reader call until the user replies; import-linter contracts make that structural.

## Alternatives considered

- **World webhook**: outside the fixed contracts.
- **Check when the user next speaks**: not proactive.
- **Planner run for the follow-up**: more flexible wording, but the decision is policy, and a code path is deterministic and free.
- **Follow-up in the origin conversation**: collides with any errand open there.

## Consequences

- Polling adds `list_bookings` calls (cheap in the mock).
- The original approval is long consumed, so a rebook structurally needs a fresh one.
