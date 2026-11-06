# ADR 0006: Memory model

## Context

Stable preferences must persist across sessions; users must be able to inspect, correct and forget them; explicit and inferred preferences must be distinguished; forgotten and unconfirmed inferred facts must never reach the model.

## Decision

- A `memories` table with a typed key registry (home area, earliest and latest time, avoid days, budget ceiling per service, preferred and avoided provider per service, language, access note, contact window), a scope (global or per service), `kind` (explicit or inferred) and `status` (active, pending_confirmation, superseded, forgotten, rejected). One active row per (user, key, scope).
- Explicit: stated by the user as a standing preference, with an evidence quote that contains the value and a standing-preference cue ("always", "from now on", "I live in"); active immediately, with Undo. Without the cue it is a brief patch for this errand only.
- Inferred: from rules over booking history or the model's `claim="inferred"`; `pending_confirmation` until the user confirms; never sent to the model while pending.
- Correction creates a new row and supersedes the old one. Every change is in an append-only `memory_events` log.
- Forget happens only on a user click (on `/memory`, a `MemoryListCard`, or the chip that the planner's `propose_forget` renders). It cascades: every brief field whose provenance points at the memory is cleared in the open errand, archived errands and `known_bookings.origin_brief_json`, and the agent asks for it again. Redaction spans come from the memory's evidence offsets plus every surface form of the value, in all roles. Then the value is nulled; only hashes survive for a blocklist and for a render-time n-gram mask over every context section.
- Memory-related assistant content (chips, suggestions, memory cards) is stored by `memory_id` reference, never as inline values.
- Inspect works in chat too: `list_memories` makes code render a `MemoryListCard` straight to the user.
- The context builder reads only the `memories_eligible` view.

## Alternatives considered

- **Vector store of free-text memories**: nondeterministic in tests and poor at expressing superseded or forgotten.
- **Forget only the memory row**: the long prior conversation would leak the fact back into context.
- **Let the model decide explicit vs inferred freely**: no evidence check, so a review could plant a "preference".
- **A model `forget_memory` tool**: "forget about the electrician, let's do plumbing" would irreversibly delete a preference on model interpretation alone.

## Consequences

- The registry has to grow deliberately; free-form notes are capped and scoped.
- Forget is irreversible after the undo window, which the UI states.
- Open for gate 2: forgetting a preference does not mask the world's booking records (architecture section 12.3).
