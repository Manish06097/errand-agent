# ADR 0007: Context budget and token estimation

## Context

The model context must be capped at a fixed token budget with 50+ memories and a long prior conversation seeded. Only relevant, current preferences may reach the model, and what was sent must be logged and proven in tests. Evals run offline.

## Decision

- A hard cap of 12,000 estimated tokens per request, split into sections whose caps sum to the hard cap (a config test fails otherwise): rules 1,600, tools 3,800 with the 400-token preamble inside it, errand state 1,100, memories 600, ledger 400, recent turns 1,000, current message 1,000, loop reserve 2,300, slack 200 (architecture section 13.1). The split is gate 2's, measured on the eval traces and applied in M8; the earlier one put the preamble outside the tools section, so it summed to 12,400, and its 2,000-token tool cap could not hold 11 tools.
- Each section sheds by its own rule when full and never borrows from another or from slack. The loop reserve is a floor, not a ceiling: the loop may use whatever the other sections leave.
- Estimator: `ceil(utf8_bytes / 1.5) + 8` per block plus 400 for the tool preamble, over every block in the request, including replayed thinking and tool_use blocks inside the tool loop. The spike measured 1.68 characters per token at worst (Hindi-mixed) and 1.89 for compact JSON; bytes are never fewer than characters, so dividing bytes by 1.5 overcounts by at least 12%. A live-only test with thinking-bearing fixtures asserts it never undercounts `count_tokens`.
- Inside the loop, the previous response's reported usage (input plus output) is a floor for the next request. If the next call would cross the cap, the loop compacts once: a fresh request rebuilt from state with tool results as text and no thinking blocks. If that still does not fit, it sheds recent turns down to the latest, then the ledger, then memories down to pinned rows. Only then does it end with `CONTEXT_EXHAUSTED`. Rules, tools, errand state and the current message are never shed.
- In replay and live modes, INV-06 also asserts the API-reported input tokens stay within the cap, and the eval table reports the maximum.
- The builder is a pure function of state, memories, messages and clock. Relevance is a gate and the score only orders: a memory is a candidate only if it is eligible, its scope is global or the brief's service, and its key applies to the errand kind; a service row shadows the global row of the same key; candidates are packed greedily by a deterministic score with fixed tie rules and rendered in registry order. Older history becomes a code-built ledger of booking-record fields; the last 6 turns go verbatim after redaction. No model ever summarises.
- `LLMGateway` refuses to send an over-cap request. Oversized user input ends with `CONTEXT_TOO_LARGE`.
- Every call writes a `context.built` manifest with included and excluded ids and a reason from a closed set for every one of the user's memories, plus the memory epoch, dropped turns, ledger lines, masking, compaction and shedding; the sanitized rendered request is on the `model.call` record. INV-20 checks the manifest against the raw request. Canary strings prove exclusion.

## Alternatives considered

- **`count_tokens` per call**: network in the evals, latency, and the same validation quirks as real requests.
- **4 characters per token**: Claude's tokenizer is much denser; it would undercount by 2x on JSON.
- **LLM rolling summary**: can invent and retain forgotten facts; not assertable.
- **Embedding retrieval**: nondeterministic.
- **Applicability as a weighted score term under a threshold**: one explicit global row cleared the threshold alone, so follow-up-only keys would ride along on every booking.
- **Keeping the tool list at 2,000 tokens**: 11 schemas would need worse descriptions. **Raising the 12,000 cap**: the gate 1 cap stays.

## Consequences

- The conservative estimator wastes some budget; I prefer that to an over-cap request.
- Fewer verbatim turns (1,000 tokens), and a compaction (one extra model call) on ordinary bookings when errand state, recent turns and the current message are all near full.
- The tool list has about 140 tokens of headroom at 11 tools; a test fails over its cap in every milestone that adds a tool.
- The cap forces tool-result compaction, which also keeps the model focused.
