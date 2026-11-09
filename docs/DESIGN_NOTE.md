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
Consuming an approval writes an intent and an attempt with a derived key before `book` is sent; `verify` asks `get_booking_status(key)` first. A `not_found` on a live hold gets one same-key retry at a provider that declares key dedupe; otherwise the user hears `PENDING_VERIFICATION` with the time the hold lapses. Before serving, and every 30 s, a reconciler reads our tables (never LangGraph's state): it verifies each unsettled attempt by its key, ends dead runs once, and expires a grant nobody acted on with a fresh card, never resuming it. A booking differing from the approval is never called booked.

## Memory
<!-- budget: 80 words -->

One typed row per fact, keys fixed in a code registry. A row is explicit only when code finds a standing-preference cue in the user's own quoted words; inferred rows come from two rules over bookings and wait for Keep. Forget is one transaction that overwrites the value in every message, nulls it, and writes HMAC hashes; the mask and the trace reader use them on read. Bookings stay. Each request is rebuilt from eligible rows only.

## Observability
<!-- budget: 70 words -->
Each run writes `data/traces/{thread}/{run}.jsonl`, the trace of record. One Tracer assigns sequence and span ids, sanitizes once (review text hashed, phones, addresses and keys masked, thinking reduced to summary and signature hash), validates schema v1 and fans the same line to every sink. Each `model.call` carries the request hash, usage split and cost; `run.end` totals are summed from those records. Langfuse, off by default, mirrors it live with our costs.

## Trade-offs
<!-- budget: 80 words -->
Most evals run on a planner I scripted, with five real-model replay cassettes and a live smoke instead of an LLM judge: exact and offline, and I say so in the table. SQLite with one writer, not Postgres: one file, simple recovery. LangGraph with one interrupt and the tool loop inside one node, so recovery reads my tables, not graph state. Holds are placed before the click: free, temporary, expiring. Traces are masked on read, never rewritten.

## Trace excerpt
<!-- budget: 40 words -->
<!-- One code block of at most 12 lines, cut from docs/traces/timeout_after_success.jsonl. -->
A live run, `book` forced to time out after the world committed it (dev drawer). I clicked, the intent was written first, and one status check by the same key found the booking. No second `book`.

```
{"seq":6,"type":"model.call","run_id":"r_01a1bf696a2d..","model":"claude-opus-5-5","effort":"medium","usage":{"input_uncached":1107,"cache_read":2733,"cache_write_5m":0,"cache_write_1h":0,"output":189,"reasoning":109},"cost_usd":0.0088,"latency_ms":4295.3683,"stop_reason":"tool_use"}
...
{"seq":3,"type":"approval.granted","approval_id":"ap_r_01a1bf696..","fingerprint":"fp1_350ce38c3c..","via":"button"}
{"seq":8,"type":"approval.consumed","approval_id":"ap_r_01a1bf696..","intent_id":"in_ap_r_01a1bf.."}
{"seq":10,"type":"intent.written","intent_id":"in_ap_r_01a1bf..","attempt":1,"idempotency_key":"bo_2a16c1ecf37..","hold_id":"H-6649"}
{"seq":13,"type":"tool.call","tool":"book","args":{"hold_id":"H-6649","idempotency_key":"bo_2a16c1ecf37.."},"error":"ToolTimeout","latency_ms":8000.857}
{"seq":17,"type":"state.transition","from":"executing","to":"verifying","reason":"book_unknown"}
{"seq":18,"type":"tool.call","tool":"get_booking_status","args":{"idempotency_key":"bo_2a16c1ecf37.."},"result":{"status":"confirmed","booking_id":"B-8863"}}
{"seq":21,"type":"intent.resolved","intent_id":"in_ap_r_01a1bf..","state":"succeeded","booking_id":"B-8863","book_calls":1}
{"seq":27,"type":"run.end","outcome":"BOOKED_VERIFIED","totals":{"model_calls":0,"tool_calls":4,"cost_usd":0.0}}
```
