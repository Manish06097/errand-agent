# ADR 0010: Simulated clock and keyed per-call randomness

## Context

Hold expiry (10 min), approval TTLs, backoff, wall-clock limits and scheduled provider cancellations all depend on time. Book outcomes are random by contract. Tests must be reproducible and fast, including across a process restart.

## Decision

- A `Clock` protocol is injected everywhere. `SimClock` stores `sim_now` in `world.sqlite`, so a restarted agent sees the same time; `sleep()` advances instantly; each world op advances time by its configured latency; timeouts advance by the gateway timeout without a real wait. The demo uses `SystemClock` plus an offset owned by the world, which the agent reads too, so both sides agree on `now`. The agent stores times as integer epoch milliseconds.
- Every random draw is `sha256(seed|op|subject|attempt)`, keyed by what it is about (the idempotency key for `book`). Scenario `forced` lists take precedence.

## Alternatives considered

- **freezegun or patching `datetime`**: global, leaks across async tasks, does not survive a subprocess restart.
- **One global `random.Random(seed)`**: every outcome depends on the order of every earlier call, so unrelated code changes reshuffle scenarios.

## Consequences

- No code may call `datetime.now()` directly; a lint rule enforces it outside `clock.py`.
- Limit-exhaustion and expiry scenarios run in milliseconds.
- A contract test checks the 70/15/7.5/7.5 split over 20,000 draws.
