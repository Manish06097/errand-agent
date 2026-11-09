# Gate 1: state model and approval design

Status: signed off on 2026-11-06, with one change (question 1, below). Nothing is built yet; M0 starts next.
Detail lives in `architecture.md` (section numbers in brackets) and the ADRs. Two design reviews raised 41 findings (3 blockers, 20 majors, 18 minors). Every blocker and major is fixed in the docs, and so are all the minors, some with a modified fix.

## Decisions

1. **LangGraph graph with one interrupt (`await_approval`); a clarifying question ends the run** - input routing stays in one function and every click finds an interrupt to resume [6] - rejected: an interrupt per question, a prebuilt ReAct agent.
2. **The model proposes, code disposes** - the planner gets read, brief, memory and `propose_*` tools only, and search arguments come from the resolved brief, so even a hijacked model can only propose [10] - rejected: a `confirm_booking` tool, an `approved` flag in graph state.
3. **An approval is a single-use DB row bound to a material fingerprint** (provider, slot, held price, booking status, nearby same-service bookings) - any material change invalidates it by construction, and a `granted` row always ends consumed, invalidated or expired [8] - rejected: binding to `hold_id`, which forces a pointless second card.
4. **A typed confirmation must name the action ("book it"), reply to the card as the latest message, and be typed after the card rendered** - a "yes" to the assistant's own side question must never book [8.3] - rejected: bare "yes"/"ok", a model classifier.
5. **Write-ahead intent and a derived idempotency key are committed before every side effect; always verify, even after a clean success, and check the booking matches what was approved** - one success path that is provable from three records [9] - rejected: trusting the `book` response, exactly-once from checkpoints.
6. **After a timeout, `get_booking_status` decides; on `not_found` with the hold still live, send `book` once more with the same key on the same hold, then check status again before reporting** - the brief makes status authoritative and the key exists so the provider dedupes the retry; at most one retry per approval, a lapsed hold means a fresh hold and a fresh approval, and a provider that does not dedupe gets the wait rule [9.4] - rejected: waiting for the hold to lapse every time (my pre-gate recommendation), a new key on a new hold without asking.
7. **Recovery reads our tables, not LangGraph's `next`** - before serving, the reconciler resolves attempts, resumes or expires granted approvals, and finishes follow-up jobs. A per-user lock spans all conversations [9.7] - rejected: per-thread locks alone, which let two chats act on one booking.
8. **The mock world is a separate SQLite store behind the 8 contracts, exactly on contract by default** - honest ground truth, and every extra failure is a named test-only fault in the eval table [5] - rejected: a shared database with the agent.
9. **Reviews go only to a tool-less Haiku reader that outputs enums; flagged reviews never help ranking** - injected text never reaches the planner [11] - rejected: a regex filter, raw reviews plus "treat as data".
10. **Memory is typed rows: explicit needs a standing-preference cue, inferred stays pending until confirmed, and forget runs only on your click and cascades into briefs and history** - forgotten facts are provably absent (canaries in every role) [12] - rejected: a vector store, a model-driven forget.
11. **A 12k-token hard cap per request, a conservative estimator checked against real usage, a code-built ledger, and loop compaction** - the budget can be proven in tests, not just claimed [13] - rejected: an LLM rolling summary, `count_tokens` on every call.
12. **E2E evals through the real API: a scripted reference planner, hostile planners and real-model replay for every brief category, with 18 invariants over trace, agent DB and world log** - shows the enforcement lives in code [18] - rejected: wording asserts, an LLM judge.

## Questions put to the gate

1. **Timeout, then `not_found`: wait for the hold to lapse, or retry at once with the same key?** Waiting costs up to 10 minutes plus a fresh click in about 7.5% of bookings. An immediate retry is only safe if the provider dedupes keys. *My recommendation was: wait.*
2. **Hold automatically when proposing, or ask before holding?** *My recommendation: automatic.* Every card then shows a real quote, and holds are free and lapse in 10 minutes. A reviewer could still read a hold as consequential, and `hold_requires_click` is one flag away.
3. **Keep typed confirmation, or buttons only?** Buttons only is simpler and removes a whole class of bugs. *My recommendation: keep the strict typed path.* Typing "book it" is natural in a chat agent, and the rules above make it deterministic.

## Decisions taken

1. **Q1: retry with the same key (changed from my recommendation).** The brief names `get_booking_status` the source of truth, and `book` takes an idempotency key precisely so the provider dedupes retries; the world honours that by default. Making the user wait up to 10 minutes and click again in about 7.5% of bookings is bad product. The rule [9.4]: on `not_found` with the hold still live, re-send `book` with the same key on the same hold, verify with `get_booking_status` again, then report. At most one retry per approval (`booking.max_same_key_retries`, configurable). If the hold has expired by then, a fresh hold and a fresh approval. If the retry is spent and status still says `not_found`, the wait rule is the fallback, which now hits about 0.56% of bookings. Proof: S23 (retry verifies), S53 (late commit: the retry returns the stored booking, not a second one), S71 (a duplicate same-key `book` is a no-op), S68 (retry spent, wait rule), S72 (hold lapsed, fresh card), plus contract tests for replay and in-progress keys. The dedupe-off world mode stays as a documented, tested degraded mode: provider dedupe is a declared capability (`booking.provider_dedupes_keys`), and without it the agent switches to the wait rule (S69, S70). I kept it because the retry rests on exactly one assumption, and that assumption should be tested both ways.

   Reconfirmed explicitly at Gate 2 on 2026-11-09 (GATE-02 question 4). The sign-off above was a bare "ok" to a changed recommendation, which was thin for the one rule M5 is built around, so I put it to the gate again. The same-key retry stands: `booking.max_same_key_retries` stays at 1, and 0 (the wait rule) is the documented alternative.
2. **Q2: automatic hold when proposing, as recommended.** `hold_requires_click` stays as a flag.
3. **Q3: keep the strict typed confirmation path alongside the buttons, as recommended.**

Updated for Q1: `architecture.md` sections 5.3 to 5.6, 6, 9.1, 9.2, 9.4, 9.6, 9.7, 15.1, 15.2, 16, 18.5 (INV-03), 18.6 (S22 to S24, S53, S58, new S68 to S72), 20 and 21; ADR 0003 and 0004; `milestones.md` M1, M5 and M6. The gate 1 ADRs move to Accepted.

## What gets built next

**M0, scaffold:** uv backend and Next shell with pinned versions. One `make dev` for world, API and web. `make check` runs ruff, mypy, an em-dash guard, an attribution guard, a secret scan and import-linter. Also in M0: the spike scripts and raw outputs go into `spikes/` with a findings README, a `DESIGN_NOTE.md` skeleton gets a word-budget check, `.gitignore` covers `data/`, and the first commit lands.

**M1, mock world:** contracts with the `Slot` and `date_range` shapes. A seeded catalog with injection reviews and the two seeded bookings. A keyed RNG and a persisted `SimClock`. All 8 tools exactly on contract, with test-only faults kept in a separate block. An `operations` table and `op_log`, scheduled provider cancels, and in-process and HTTP transports. Done means:
- the contract suite passes on both transports;
- 20,000 draws land within 1% of 70/15/7.5/7.5;
- HTTP produces a real timeout after commit, and a late commit;
- a same-key replay is a no-op, a same-key call on an in-progress key waits and replays, and a same-key call after `timeout_not_booked` draws afresh;
- with no faults on, nothing behaves outside the brief's table.

Signed off: 2026-11-06
