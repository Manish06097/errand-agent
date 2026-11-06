# ADR 0011: Concurrency and run lifecycle

## Context

The spike showed two concurrent resumes on one thread both ran `book`. A dropped HTTP connection must never cancel a booking in flight. The UI needs streaming tokens, trace records and cards.

## Decision

- `RunManager` runs each graph invocation as a detached task; POSTs return a `run_id`; the browser subscribes to `GET /api/runs/{id}/events` (SSE) with replay from the JSONL via `Last-Event-ID`.
- One active run per thread. Input arriving during a run gets 409 `run_in_progress` and is queued in the UI, with `visible_card` and `client_composed_at` captured at Send so a queued confirmation cannot bind to a later card.
- Per-thread `asyncio.Lock` plus a `leases` row (conditional UPDATE, 5 s renewal) so a second worker process is safe. Every takeover increments an epoch that fences attempt-state writes.
- A per-user consequential lock around `execute` and `verify`, because two conversations can hold cards for the same booking or the same replacement; per-thread locks alone cannot stop them both acting.
- Under the lock the API checks for a pending interrupt and chooses `Command(resume=...)` or plain input.
- `client_msg_id` and `client_action_id` make POSTs idempotent.

## Alternatives considered

- **SSE on the POST response**: simple, but tying the run to the request means a closed tab can cancel work.
- **WebSockets**: more machinery for request-scoped push; no built-in replay.
- **Rely on idempotency alone**: correct, but the lock keeps the trace and the user experience clean; idempotency stays as the backstop.

## Consequences

- A separate user-level SSE stream carries notifications.
- The lease adds a row write per run; negligible.
