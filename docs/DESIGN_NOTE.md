# Errand agent: design note

<!--
One page. Each section is written when the milestone that builds its topic lands, and
must stay within the budget in its comment. scripts/check_design_note.py enforces the
per-section budgets, the 550-word total and the 12-line trace excerpt in `make check`
and in pre-commit. The long-form design is docs/design/architecture.md.
-->

## State model
<!-- budget: 90 words -->
One LangGraph thread per conversation holds one errand: a typed brief where every field carries provenance (the user's quoted words, checked by code), a code-built shortlist, and a phase that only `transition()` changes, against a fixed table. Checkpoints are strict msgpack, so only types reachable from `ThreadState` load. The transcript, runs and leases live in agent.sqlite; thinking blocks never reach state. The planner proposes patches and questions; code merges, resolves dates, derives search arguments and caps repeat questions.

## Approval
<!-- budget: 90 words -->
The model only proposes. Code holds the slot, prices it, and shows a card whose fingerprint hashes the material facts (user, provider, service, slot, held quote, nearby same-service bookings). A grant is a button press carrying that fingerprint, or a typed "book it" bound to the latest rendered card and message. Consumption is one conditional UPDATE, then `book` runs once under a write-ahead key and `verify` re-reads the world before anything is reported. Stale, expired, repeated or ambiguous input never grants; a claim guard strips unbacked "booked" text.

## Idempotency and recovery
<!-- budget: 100 words -->
Consuming an approval writes an intent and an attempt with a derived key before `book` is sent; a new attempt is allowed only after every earlier one is provably not applied. `verify` always asks `get_booking_status(key)` first. A `not_found` on a live hold gets one same-key retry at a provider that declares key dedupe, then another status check; otherwise the user hears `PENDING_VERIFICATION` with the time the hold lapses, because the request may still land. A booking that differs from what was approved is never called booked. A lapsed hold is held again once; a changed quote invalidates the card.

## Memory
<!-- budget: 80 words -->

## Observability
<!-- budget: 70 words -->
Each run writes `data/traces/{thread}/{run}.jsonl`, the trace of record. One Tracer assigns sequence and span ids, sanitizes once (review text hashed, phones, addresses and keys masked, thinking reduced to summary and signature hash), validates schema v1 and fans the same line to every sink. Each `model.call` carries the request hash, usage split and cost; `run.end` totals are summed from those records. Langfuse, off by default, mirrors it live with our costs.

## Trade-offs
<!-- budget: 80 words -->

## Trace excerpt
<!-- budget: 40 words -->
<!-- One code block of at most 12 lines, cut from docs/traces/timeout_after_success.jsonl. -->
