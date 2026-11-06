# ADR 0007: Context budget and token estimation

## Context

The model context must be capped at a fixed token budget with 50+ memories and a long prior conversation seeded. Only relevant, current preferences may reach the model, and what was sent must be logged and proven in tests. Evals run offline.

## Decision

- A hard cap of 12,000 estimated tokens per request, split into sections (rules, tools, errand state, memories, ledger, recent turns, current message, loop reserve, slack; architecture section 13.1).
- Estimator: `ceil(utf8_bytes / 1.5) + 8` per block plus 400 for the tool preamble, over every block in the request, including replayed thinking and tool_use blocks inside the tool loop. The spike measured 1.68 characters per token at worst (Hindi-mixed) and 1.89 for compact JSON; bytes are never fewer than characters, so dividing bytes by 1.5 overcounts by at least 12%. A live-only test with thinking-bearing fixtures asserts it never undercounts `count_tokens`.
- Inside the loop, the previous response's reported usage (input plus output) is a floor for the next request. If the next call would cross the cap, the loop compacts: a fresh request rebuilt from state with tool results as text and no thinking blocks. Only if that still does not fit does it end with `CONTEXT_EXHAUSTED`.
- In replay and live modes, INV-06 also asserts the API-reported input tokens stay within the cap, and the eval table reports the maximum.
- The builder is a pure function of state, memories, messages and clock. Memories are filtered by the eligibility view, scope and a deterministic score; older history becomes a code-built ledger; the last 6 turns go verbatim after redaction.
- `LLMGateway` refuses to send an over-cap request. Oversized user input ends with `CONTEXT_TOO_LARGE`.
- Every call writes a `context.built` manifest with included and excluded ids and reasons; the sanitized rendered request is on the `model.call` record. Canary strings prove exclusion.

## Alternatives considered

- **`count_tokens` per call**: network in the evals, latency, and the same validation quirks as real requests.
- **4 characters per token**: Claude's tokenizer is much denser; it would undercount by 2x on JSON.
- **LLM rolling summary**: can invent and retain forgotten facts; not assertable.
- **Embedding retrieval**: nondeterministic.

## Consequences

- The conservative estimator wastes some budget; I prefer that to an over-cap request.
- Compaction costs an extra model call now and then; capping `max_tokens` per call instead would truncate thinking.
- The cap forces tool-result compaction, which also keeps the model focused.
