# ADR 0003: Write-ahead intents, derived idempotency keys, status-first recovery

## Context

Duplicate bookings must be prevented across repeated confirmations, network retries and restarts. On a timeout the source of truth must be queried before retrying or reporting success, and success must never be claimed before verification. The brief names `get_booking_status` the authoritative source for ambiguous booking outcomes and gives `book` an idempotency key, which is how a provider dedupes a retried request. The spike proved LangGraph nodes are at-least-once and that a crash after the provider books re-runs the booking node.

## Decision

- `action_intents` (one per consumed approval, `UNIQUE(approval_id)`) and `intent_attempts` (one per provider-side attempt, `UNIQUE(idempotency_key)`). The approval consume, intent insert and first attempt insert share one `BEGIN IMMEDIATE` transaction committed **before** the call.
- Key = `derive(action, approval_id, subject)` where subject is the hold id (book), booking id (cancel) or booking id plus new slot (reschedule). Persisted and always read back; derivation is a second guard.
- Every non-definitive result, including a clean `confirmed`, moves to `verify`, which calls `get_booking_status(key)` with bounded backoff and checks that the confirmed booking matches the approved material. Nothing is reported as a success before that.
- **Timeout, then `not_found` (decided at gate 1).** If the hold is still live by the world clock and the provider dedupes keys, `verify` re-sends `book` once with the same key on the same hold, then checks status again, then reports. The retry is bounded per approval (`booking.max_same_key_retries`, default 1) and committed write-ahead like the first send. It is not a new attempt and needs no new approval, because at a deduping provider it can only complete the request the user approved: an applied key replays its stored booking, and an in-progress key waits for the original.
- If the hold has expired by then, `not_found` is definitive (a late request can only get `hold_expired`), and the next booking needs a fresh hold and a fresh approval. After an ambiguous result I never re-hold automatically under the old approval, and `book` never goes out on a hold with any key but the attempt's own.
- If the retry is spent and status still says `not_found` on a live hold, or the provider does not dedupe keys, the wait rule applies: no further `book` on that hold, `PENDING_VERIFICATION` with the lapse time, and `not_found` becomes definitive only once the hold lapses by the world clock. "Never mind" spends any retry that is left.
- A new attempt (new hold, new key) under the same approval is allowed only when every earlier attempt is `not_applied` by an answer the world returned to a first send (`slot_taken`, `hold_expired`) or because it is provably unsent (`prepared`). That is the unchanged re-hold path.
- Provider dedupe is a declared capability, `booking.provider_dedupes_keys` (default true), not an assumption. The world's `enforce_idempotency: false` mode stays as a tested degraded mode in which the agent switches to the wait rule; the eval harness derives the agent setting from the scenario, so the two cannot disagree in a test.
- Once `PENDING_VERIFICATION` has been shown, the agent never re-books with a new hold or key automatically. A definitive "not applied" leads to a fresh card.
- Cancel and reschedule are verified through `list_bookings`, as the contract provides; both are effect-idempotent, so a same-key retry is safe for them.
- `execute` and the reconciler both start with the intent table: an unresolved attempt goes to `verify` first, and the retry rules above are the same in both.
- On startup, before serving, the reconciler resolves every attempt in `prepared`, `in_flight` or `unknown`, resumes or expires every granted-but-unconsumed approval (from `pending_resumes`), resumes unfinished follow-up jobs, marks the original runs `recovered`, then resumes other interrupted runs with `ainvoke(None)`. Every 30 s it repeats this for rows whose run lease has expired, with lease epochs fencing attempt writes.
- Cancel's transient 503 is retried inside the attempt with the same key, as explicit, traced calls.

## Alternatives considered

- **Exactly-once from checkpoints**: disproved by the spike.
- **Random key per click or per attempt**: defeats dedupe on retry.
- **Key from (user, provider, slot)**: a legitimate re-book after a cancel would dedupe onto the cancelled booking.
- **Retry immediately on timeout, before checking status**: skips the source of truth the brief requires, and at a provider that does not dedupe it is the classic double booking.
- **Always wait for the hold to lapse after `not_found`** (my recommendation before the gate): provable without provider dedupe, but it costs up to 10 minutes and a second click in about 7.5% of bookings, and it ignores both the authoritative status check and the key the brief gives `book`. It survives as the fallback when the retry is spent or the provider does not dedupe.
- **Unbounded same-key retries**: each one is cheap, but a stuck provider would turn one click into a loop the user cannot see. One retry captures almost all of the value: the wait case drops from 7.5% to about 0.56% of bookings in the default distribution.
- **Re-hold and send a new key after `not_found`**: a new key is a new request, so a late original could land next to it. That needs a fresh approval.
- **Drop the `enforce_idempotency: false` mode**: it would leave the retry's one assumption untested and the fallback unexercised. I keep it because real providers differ, and the agent should degrade to a safe rule rather than rely on a property nobody checked.
- **Trust a clean `book` success**: two success paths instead of one, and one less invariant.
- **LangGraph `RetryPolicy` for consequential calls**: hides attempts from our trace.

## Consequences

- One extra status call per booking, which the mock makes cheap and which a real provider would tolerate; a second `book` and status call in the `timeout_not_booked` case.
- After a `timeout_not_booked` the user almost always still makes one click and gets a verified answer in the same run. Only a second `timeout_not_booked` on the retry (about 0.56% of bookings) waits up to 10 minutes and needs a fresh card.
- The retry's safety depends on provider dedupe, which the brief's key implies and the world honours by default. The dependency is explicit (`provider_dedupes_keys`) and proven: contract tests show a replay is a no-op and an in-progress key waits, S53 shows a late commit comes back as the stored booking, and S71 shows a duplicate delivery changes nothing. The dedupe-off scenarios (S69, S70) prove the fallback.
- INV-03 checks a shape rather than a single call: same key, same hold, a `not_found` on a live hold before every re-send, a status check after it, the count bound, and one world booking per key.
- The reconciler must run before the API serves, which adds a little startup latency.
