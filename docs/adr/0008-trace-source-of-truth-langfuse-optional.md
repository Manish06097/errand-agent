# ADR 0008: Our JSONL trace is the source of truth; Langfuse is an optional sink

## Context

Each run needs a trace of state transitions, model calls, tool names and arguments, sanitized results, latency, tokens and cost. One raw trace is a deliverable, the evals must run offline, and the demo shows chat and trace side by side.

## Decision

- Our `Tracer` assigns ids, sanitizes once, and computes latency and cost from our price table. `JsonlSink` always writes first (`data/traces/{thread}/{run}.jsonl`, fsync on approval, intent and run-end records). `StreamSink` feeds the live trace panel from the same records. `LangfuseSink` is constructed only when keys are set and `ERRAND_LANGFUSE` is not off.
- Langfuse spans are emitted live with a private `TracerProvider`, explicit `cost_details`, and `trace_id` derived from our run id. No LangChain `CallbackHandler` in the product path. `shutdown()` only in the lifespan exit.
- Traces are written per run, and `errand-trace export --thread` concatenates every run of a conversation into one file. The end-to-end deliverable is that conversation-level file, because the approval click is its own run and a single run file would have no user request or model call.
- Langfuse runs self-hosted from a pinned compose file plus an override (port 3100, telemetry off).

## Alternatives considered

- **Langfuse as the record**: online only, 15 to 30 s ingestion lag, cannot backfill timestamps, and an external dependency in the evals.
- **CallbackHandler**: duplicates model calls and sends raw graph state, including thinking blocks, past the sanitizer.
- **LangGraph checkpoints as the trace**: msgpack blobs, not human-readable, and missing tool and cost detail.

## Consequences

- I own the trace schema and its versioning (`v` field).
- The demo shows our trace panel as primary and Langfuse as secondary because of ingestion lag.
- The app works with Docker off.
