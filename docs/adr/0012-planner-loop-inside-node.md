# ADR 0012: Planner tool loop inside one node, context rebuilt per invocation

## Context

Opus 5.5 thinking blocks are bound to the model and the exact conversation prefix; editing earlier history, including the system prompt, is a hard 400 on this account. The spike validated two safe patterns: append-only within one tool loop, and rebuilding each new turn from our own state with prior turns as text and no thinking blocks. It also recommended never storing thinking blocks in SQLite.

## Decision

- The read-only tool loop runs inside the `plan` node. Within it, system prompt, tool list and earlier messages are byte-identical across calls; tool results are sanitized before they are appended, never after; any per-call note is appended after the last tool result.
- Each `plan` invocation rebuilds context from our state (`ContextBuilder`), so memory and errand state can change freely between turns.
- Only `PlanResult` is written to graph state. Raw thinking lives only in the JSONL trace (summary sanitized, signature hashed).
- Live runs send `prefix_mismatch_behavior: "error"` so a violation fails loudly.
- When the next in-loop request would cross the context cap (measured from reported usage, because replayed thinking counts as input), the loop compacts into a fresh request rebuilt from state, which is the second safe pattern.

## Alternatives considered

- **`plan -> tools -> plan` graph cycle with checkpointed loop messages**: nicer graph spans, but checkpoints thinking blocks and gains nothing for read-only tools.
- **`drop_block` in production**: hides bugs.

## Consequences

- A crash mid-plan repeats reads and model spend; I accept the cost.
- Per-call trace spans come from our tracer, not from LangGraph node boundaries.
