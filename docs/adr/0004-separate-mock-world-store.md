# ADR 0004: Mock world as a separate store behind the tool contract

## Context

The tools must be mocks with fixed contracts and reproducible, configurable behaviour. The evals need ground truth that does not depend on what the agent believes.

## Decision

- The world lives in its own package (`errand.world`, which imports nothing from the agent) and its own `world.sqlite`.
- The agent reaches it only through `WorldClient` and the eight contracts. Two transports run the same engine code: in-process for fast evals, HTTP (`errand-world`, port 8765) for the demo and restart tests.
- The world keeps an `operations` idempotency table, an `op_log` of every call with its random draw, scheduled events, and the simulated clock.
- By default the world dedupes `book` keys the way the brief's idempotency key implies: a key is claimed when the request is accepted, an applied key replays its stored response, a same-key call on an in-progress key waits for it, and a `not_applied` key is evaluated afresh. The agent's bounded same-key retry (ADR 0003) relies on exactly this, and the contract suite proves it on both transports.
- `enforce_idempotency: false` models a provider that does not dedupe: it disables the replay and the in-progress wait. It is a documented degraded mode, not only a test switch: the agent's `booking.provider_dedupes_keys` mirrors it, and with it off the agent uses the hold-lapse wait rule. The world still records every operation by key, so `get_booking_status(key)` works in both modes.
- The default world is exactly on contract. Extra failure behaviour (always-503 cancel, status outage, malformed responses, late commits, duplicate delivery, slot shifts on re-hold) exists only as named test-only faults in a separate `faults` block, and the eval table shows which scenarios use them. Holds never supersede each other, so `hold_expired` means only "past expiry".

## Alternatives considered

- **World tables inside `agent.sqlite`**: convenient, but recovery code could "resolve" a timeout by peeking at the table instead of calling `get_booking_status`, and the timeout-after-success failure could become accidentally atomic.
- **Pure in-memory mock**: does not survive the restart tests and has no inspectable ground truth.

## Consequences

- Invariants can compare three independent records: trace, agent DB, world `op_log`.
- Restart tests kill the agent while the world keeps running, which matches reality.
- A contract test suite runs against both transports so they cannot drift.
