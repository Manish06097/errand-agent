# Errand agent: design note

<!--
One page. Each section is written when the milestone that builds its topic lands, and
must stay within the budget in its comment. scripts/check_design_note.py enforces the
per-section budgets, the 550-word total and the 12-line trace excerpt in `make check`
and in pre-commit. The long-form design is docs/design/architecture.md.
-->

## State model
<!-- budget: 90 words -->

## Approval
<!-- budget: 90 words -->

## Idempotency and recovery
<!-- budget: 100 words -->

## Memory
<!-- budget: 80 words -->

## Observability
<!-- budget: 70 words -->
Each run writes `data/traces/{thread}/{run}.jsonl`, the trace of record. One Tracer assigns sequence and span ids, sanitizes once (review text hashed, phones, addresses and keys masked, thinking reduced to summary and signature hash), validates schema v1 and fans the same line to every sink. Each `model.call` carries the request hash, usage split and cost; `run.end` totals are summed from those records. Langfuse is an optional view.

## Trade-offs
<!-- budget: 80 words -->

## Trace excerpt
<!-- budget: 40 words -->
<!-- One code block of at most 12 lines, cut from docs/traces/timeout_after_success.jsonl. -->
