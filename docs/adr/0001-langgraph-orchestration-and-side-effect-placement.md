# ADR 0001: LangGraph orchestration and where side effects live

## Context

The brief asks for an explicit workflow, approval before consequential actions, and safe recovery. The LangGraph spike showed four things that shape the design: default `durability="async"` loses checkpoints on a crash; a crashed or resumed node re-runs from its first line; two concurrent resumes on one thread both run the side effect; and plain input on an interrupted thread silently drops the interrupt and restarts from START.

## Decision

- An explicit `StateGraph` with nodes `ingress`, `plan`, `prepare_proposal`, `await_approval`, `execute`, `verify`, `followup`, `respond` (architecture section 6.2). Edges are pure functions of state and of the type of the planner's result. The model never routes.
- **One interrupt site**: `await_approval`, which contains nothing but `interrupt()`. Clarifying questions end the run instead.
- Side effects live only in nodes that are safe to re-run because our own rows guard them: `hold_slot` in `prepare_proposal` (reused `holds` row), `book`/`cancel`/`reschedule` in `execute` (write-ahead intent, ADR 0003). Nothing consequential happens before the interrupt in the same node.
- Every input, from any source, goes through `ingress`. Recovery inputs (`Recover`, "Check now") go to `verify`; a user message goes to `plan` even while an intent is unresolved, so the user is never ignored, and validation refuses any new proposal that matches the unresolved intent. The API sends input as `Command(resume=...)` when an interrupt is pending and as plain input otherwise.
- From the moment an intent exists, `execute -> verify -> respond` is a critical section: Stop and the general caps are deferred, and every exit ends verified or in `PENDING_VERIFICATION`.
- `respond` routes back to `await_approval` whenever a live pending approval exists, so a thread with a pending card always ends its run parked at the interrupt. If anything was said after the card, `respond` re-posts a code-rendered reminder of it as the last message, so the card a typed confirmation can bind to is always the latest one.
- Every run uses `durability="sync"`, `version="v2"`, `recursion_limit=40`; strict msgpack is on.

## Alternatives considered

- **Prebuilt ReAct agent** (`create_react_agent`): hides the loop, so I cannot place the interrupt, budget checks or the append-only rule where they need to be.
- **Hand-rolled state machine without LangGraph**: fully controllable, but I would rebuild checkpointing, interrupts and streaming. LangGraph gives those, and its weaknesses (at-least-once nodes) are covered by our own tables either way.
- **`interrupt()` per clarifying question**: every free-text reply would have to be routed as a resume, which is exactly the fragile routing the spike exposed.
- **Hold or book inside the approval node**: the node re-runs from its top on resume, so the side effect would run twice.

## Consequences

- LangGraph state is never the guard for a side effect. `next` is advisory; the intent and approval tables are authoritative.
- Routing logic is concentrated in `ingress` and is easy to test.
- One more node hop (`respond -> await_approval`) per card, which is cheap.
- INV-08 (legal transitions) and INV-15 (pending approval implies parked at interrupt) run after every eval scenario.
