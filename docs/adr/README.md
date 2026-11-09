# Architecture decision records

One short record per load-bearing decision. Each one states the context, the decision, the alternatives I considered and the consequences I accept. The long-form design is `docs/design/architecture.md`; section numbers below refer to it. Statuses move from Proposed to Accepted when the matching gate record (`docs/design/GATE-0N.md`) is signed off. Measurements cited as "the spike" are reproducible from `spikes/`.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-langgraph-orchestration-and-side-effect-placement.md) | LangGraph orchestration, one interrupt, side effects only downstream of approval | Accepted (gate 1, 2026-11-06) |
| [0002](0002-approval-bound-to-material-fingerprint.md) | Approval is a DB row bound to a material fingerprint, granted only by the user | Accepted (gate 1, 2026-11-06) |
| [0003](0003-write-ahead-idempotency-and-recovery.md) | Write-ahead intents and attempts, derived keys, status-first recovery, one bounded same-key retry | Accepted (gate 1, 2026-11-06) |
| [0004](0004-separate-mock-world-store.md) | Mock world is a separate store behind the tool contract | Accepted (gate 1, 2026-11-06) |
| [0005](0005-quarantined-review-reader.md) | Untrusted reviews go only to a tool-less Haiku reader with enum output | Accepted (gate 1, 2026-11-06) |
| [0006](0006-memory-model.md) | Typed, keyed memory with explicit vs inferred, supersede, forget with redaction | Accepted (gate 2, 2026-11-09) |
| [0007](0007-context-budget-and-token-estimation.md) | 12k-token hard cap, sectioned budget, conservative byte estimator, manifest | Accepted (gate 2, 2026-11-09) |
| [0008](0008-trace-source-of-truth-langfuse-optional.md) | Our JSONL trace is the record; Langfuse is an optional live sink | Accepted (gate 1, 2026-11-06) |
| [0009](0009-offline-eval-strategy.md) | E2E scenarios with scripted reference and hostile policies plus recorded cassettes | Accepted (gate 1, 2026-11-06) |
| [0010](0010-simulated-clock-and-keyed-rng.md) | Injected, persisted simulated clock and keyed per-call RNG | Accepted (gate 1, 2026-11-06) |
| [0011](0011-concurrency-and-run-lifecycle.md) | Detached runs, per-thread lock plus DB lease, SSE with replay | Accepted (gate 1, 2026-11-06) |
| [0012](0012-planner-loop-inside-node.md) | Planner tool loop inside one node, context rebuilt per invocation | Accepted (gate 1, 2026-11-06) |
| [0013](0013-persistence-layout.md) | Three SQLite files: agent, checkpoints, world | Accepted (gate 1, 2026-11-06) |
| [0014](0014-proactive-followup-watcher.md) | Provider cancels detected by a list_bookings watcher, code-driven follow-up | Proposed (gate 3) |
| [0015](0015-code-owned-dates-ranking-and-outcomes.md) | Code resolves dates, ranks candidates and writes outcome messages | Accepted (gate 1, 2026-11-06) |
