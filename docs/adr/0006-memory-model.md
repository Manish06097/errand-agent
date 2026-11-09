# ADR 0006: Memory model

## Context

Stable preferences must persist across sessions; users must be able to inspect, correct and forget them; explicit and inferred preferences must be distinguished; forgotten and unconfirmed inferred facts must never reach the model.

## Decision

- A `memories` table with a typed key registry in code (home area, earliest and latest time, avoid days, budget ceiling per service, preferred and avoided provider per service, language, access note, contact window), each key with its value type, scopes, pinning, what it feeds, the errand kinds it applies to and the rule that may infer it (architecture section 12.1). A scope (global or per service), `kind` (explicit or inferred) and `status` (active, pending_confirmation, superseded, forgotten, rejected). One active row per (user, key, scope). Unknown keys and badly typed values are tool errors.
- Explicit: stated by the user as a standing preference. Code checks it: the evidence span from the current user message contains the value and a standing-preference cue ("always", "from now on", "I live in", key-specific cues such as "I moved to"), and no one-off marker in the same span ("this time", "today", "for now", "just for") vetoes it. Active immediately, with Undo. Without the cue it is a brief patch for this errand plus a "Remember this for next time?" chip, whose click is the cue. An add or edit on `/memory` is explicit too.
- Inferred: only from code rules over booking records (`memory/inference.py`), with booking ids as evidence. `remember` has no `claim` argument, so the model cannot write an inferred row or classify a write, and a hijacked planner cannot plant a suggestion. Rows stay `pending_confirmation`, outside the eligible view, until the user clicks Keep; Dismiss rejects the row and blocklists (key, scope, value hash) for inference. Both are conditional updates and a stale click gets 409. No suggestion is made while an active row or its contradicting pair exists, and an explicit write rejects a pending suggestion for the same key and scope.
- Undo on a "Saved" chip rejects the row and redacts nothing: it means "not a standing preference", so the user's words still count for this errand.
- Correction creates a new row and supersedes the old one in one transaction; the old row's evidence span is hidden from the model view, archived briefs are not rewritten. Every change is in an append-only `memory_events` log.
- Memory text is data: values are typed and re-validated when rendered, and the one free-text key (`access_note`) is never inferred, never read by code and rendered inside a data envelope.
- Forget happens only on a user click (on `/memory`, a `MemoryListCard`, or the chip that the planner's `propose_forget` renders) and is final in `agent.sqlite`. One transaction redacts the evidence span and every surface form of the value in user and model text written before the forget, overwriting it in `messages`; clears every brief field whose provenance points at the memory in archived errands and `known_bookings.origin_brief_json`; invalidates pending cards built from it; forgets its superseded ancestors; bumps a per-user memory epoch; then nulls the value. Only HMAC-keyed hashes survive, for the blocklist and a render-time n-gram mask over earlier user and model text (not code-rendered records, not world tool results, not the current message).
- The open errand lives in the checkpoint file, so it cannot share that transaction: the builder treats a brief field whose memory is no longer eligible as missing, `ingress` scrubs it on the next run, and every write of a brief into `agent.sqlite` goes through the same scrub. A run in flight sees the epoch change and rebuilds its next request from state.
- Bookings stay: forgetting a preference does not touch world bookings, `known_bookings`, `list_bookings` results or ledger lines.
- Outside `agent.sqlite` (JSONL traces, older checkpoints, Langfuse) a forgotten value is masked on every user-facing read through `trace/reader.py`, never rewritten.
- Memory-related assistant content (chips, suggestions, memory cards) is stored by `memory_id` reference, never as inline values.
- Inspect works in chat too: `list_memories` makes code render a `MemoryListCard` straight to the user.
- The context builder reads only the `memories_eligible` view.

## Alternatives considered

- **Vector store of free-text memories**: nondeterministic in tests and poor at expressing superseded or forgotten.
- **Forget only the memory row**: the long prior conversation would leak the fact back into context.
- **Let the model decide explicit vs inferred**: no evidence check, so a review could plant a "preference". Gate 2 also removed the model's `claim="inferred"` path.
- **A model `forget_memory` tool**: "forget about the electrician, let's do plumbing" would irreversibly delete a preference on model interpretation alone.
- **Masking booking records on forget**: breaks "same electrician as last time" for a record that is the provider's, not a preference.
- **Rewriting traces and pruning checkpoints on forget**: breaks the request hashes and the append-only trace the evals read; editing checkpoints of a thread parked at an interrupt is fragile.

## Consequences

- The registry has to grow deliberately; free-form notes are capped and scoped.
- Forget is irreversible after the 8-second undo window, which the UI states, and the undo lives in the browser.
- The raw files under `data/` (traces, older checkpoints) and Langfuse, when on, still hold a forgotten value until deleted; the README says so. The hashes stop re-display, not someone holding both `agent.sqlite` and the key file.
- The mask over-masks inside its scope: an old mention of a forgotten area about something else is masked too.
- Migration 0004 adds the surface-hash table, `approvals.memory_ids_json`, the per-user memory epoch and a kind on `message_redactions`.
