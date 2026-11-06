# Build plan

Each milestone is small enough to build and test in one focused session, ends with a green `make check` and `make evals`, and is committed on its own. Scenario ids (S01 to S72, SWEEP) and invariant ids (INV-01 to INV-18) refer to `docs/design/architecture.md` section 18.

Rules that apply to every milestone:

- Product behaviour is guarded by end-to-end scenarios through the real API, not by unit tests. Unit tests are only for pure functions where they are the clearest proof (fingerprint canonicalisation, `timewin`, RNG distribution).
- A scenario is added to the YAML catalogue in the milestone that makes it pass, and it stays in the gate from then on. The eval table grows as the build progresses.
- Every real bug is reproduced end to end first (`backend/tests/regressions/test_bug_00N_*.py` or a scenario) and the failing repro is committed on its own, then the fix is a separate commit, then the same repro is re-run. The entry goes into `docs/BUGS.md` the same day with both SHAs.
- The design note section for a milestone's topic is filled when the milestone lands, within its word budget.
- Reviews are stripped from the planner's view from the first milestone that calls `get_provider`. The reader adds the digest later; raw text is never in planner context, not even temporarily.
- No em dashes, no AI attribution in code, docs or commits (enforced by pre-commit from M0).

Design gates are where I stop and sign off before the next milestone starts. Each gate leaves a record in `docs/design/GATE-0N.md`:

| Gate | When | What I decide |
|---|---|---|
| Gate 1 | before M0 (signed off 2026-11-06) | state model, graph, approval design, idempotency, persistence, eval strategy (architecture section 20) |
| Gate 2 | after M10, before M11 | memory registry, forget semantics, relevance scoring, context budget split, seed data |
| Gate 3 | after M13, before M14 | follow-up notify policy, replacement rules, card and notification UX |
| Gate 4 | after M17 | design note, eval table, bugs write-up, trace excerpt, demo script before submission |

---

## M0. Repository scaffold and hygiene

Scope:
- `backend/` uv project (Python 3.11) with ruff, mypy, pytest, pytest-asyncio; pinned langgraph 1.2.20, langgraph-checkpoint-sqlite 3.1.3, langchain-core 1.7.6, langchain-anthropic 1.7.10, aiosqlite 0.22.1.
- `web/` Next.js 16 and React 19 app shell (pinned exact versions), Tailwind, dev server on 3200 with `/api/*` rewrites to 8000.
- `Makefile` targets: `dev` (world, API and web in one command), `check`, `evals`, `e2e`, `record`, `langfuse-up`.
- Pre-commit: ruff, mypy, an em-dash guard over tracked text, an attribution guard on commit messages, a secret scan, import-linter contract (empty package skeleton so the contract already exists).
- `.gitignore`: add `data/`; scope the Next build ignore to `web/out/` so `docs/eval_results.json` and backend outputs are not swallowed by a global `out/`.
- `spikes/`: commit the pre-design experiments (LangGraph, Claude API, Langfuse scripts and their raw outputs, no SQLite files) with a README that lists each finding, the script that shows it and the raw number. Scan them for secrets first.
- `docs/DESIGN_NOTE.md` skeleton with a word budget per section (state model, approval, idempotency and recovery, memory, observability, trade-offs, trace excerpt) and `scripts/check_design_note.py` wired into `make check`.
- `.env.example`, README skeleton, `docs/BUGS.md` template (with columns for the repro SHA and the fix SHA).
- First commit.

Acceptance:
- `make check` passes on a clean clone.
- The em-dash guard rejects a staged file containing U+2014; the attribution guard rejects a commit message with an authorship trailer; the secret scan rejects a file containing an `sk-ant-` key.
- The design-note check fails on a note over budget.
- `pnpm dev` serves the shell on 3200.

## M1. Mock world

Scope: `errand.world` package: contracts (including the `Slot` and `date_range` wire shapes), catalog generator, keyed RNG, `ScenarioConfig` with the `world` (contract) and `faults` (test-only) blocks kept apart, `SimClock` persisted in `world.sqlite`, the eight tools with every failure mode from architecture section 5.6, `operations` idempotency (key claimed on receipt, replay of applied keys, wait on in-progress keys, fresh draw for a `not_applied` key, and the enforce flag for the no-dedupe mode), `op_log`, scheduled events, HTTP server with `/dev` controls, `WorldClient` with both transports, seeded bookings (completed E4 electrician, upcoming A2 AC servicing).

Acceptance:
- `tests/contract/` passes against both transports: shapes, `date_range` validation, 3 to 8 providers, at least one injection review per catalog, hold TTL, `hold_expired` only past expiry, no hold supersede, idempotent replay is a no-op (one booking, identical response, `op_log` outcome `replayed`), a same-key call on an in-progress key waits and replays (HTTP late commit), a same-key call after `timeout_not_booked` draws afresh, `faults.duplicate_delivery` is a no-op with dedupe on, key-reuse rejection, dedupe off executes again and still resolves `timeout_booked` by key, cancel 503 exactly once then success, reschedule conflict, provider-cancel event firing, price drift on hold and on re-hold.
- With an empty `faults` block, no tool produces any behaviour outside the brief's table (a contract test walks every tool).
- Demo clock: after `/dev/clock/advance`, agent-side and world-side `now` agree within one second.
- Distribution test: 20,000 draws within ±1% of 70/15/7.5/7.5.
- HTTP `timeout_booked` produces a real client timeout after the server commits; `faults.commit_latency_ms` produces a commit after the client timeout.

## M2. Foundations: store, tracer, model gateway

Scope:
- `agent.sqlite` migrations for every table in architecture section 4.1, repository layer, `BEGIN IMMEDIATE` helper.
- `Tracer` with JSONL and stream sinks, sanitizer, record schema v1, `errand-trace show`.
- `LLMGateway` with `ChatAnthropic` construction, `ScriptedChatModel` (rejects forced tool choice, records every request), cassette record/replay, pricing and token estimator, budget assertion.

Acceptance:
- A scripted two-call tool loop produces `model.call` records with usage and cost matching the spike formula, including cache-read subtraction.
- `est_request` property test: monotonic, counts replayed thinking and tool_use blocks, and is never below the spike's measured worst ratio on fixtures. `next_call_floor` uses reported usage.
- Cassette replay round-trips; a changed request fails with a readable diff.
- Socket guard and Langfuse-off fixture in `conftest.py` fail a test that opens a non-loopback socket.

## M3. Graph skeleton, API and eval harness

Scope:
- `ThreadState`, `transitions.py`, graph with `ingress`, `plan` (in-node read-only loop over `search_providers` with no model args, `get_provider` view without reviews, `list_bookings`, `update_brief`, `ask_user`), `respond`.
- Minimal context builder (rules, errand state, recent turns, current message) with the hard cap.
- `brief_ops` (`REQUIRED`, `missing`, `merge` with evidence check), `ask_user` guard with the one re-ask rule.
- FastAPI: conversations, messages, run SSE with replay, `RunManager`, per-thread lock and lease, `client_msg_id` dedupe.
- Eval harness: YAML DSL, `SimUser`, reference policy, invariant library (INV-07, INV-08, INV-11, INV-13 wired), report plugin generating `docs/EVAL_TABLE.md`.

Acceptance:
- S02, S03, S30, S46, S67 pass. INV-07, INV-08, INV-11, INV-13 hold on all of them, and INV-10 too: reviews are stripped from the planner's view from the first milestone that calls `get_provider`, which is this one.
- A test asserts the `search_providers` call's `service`, `area` and `date_range` equal the resolved brief, whatever the scripted planner tries.
- `docs/EVAL_TABLE.md` is generated by `make evals`.
- A dropped SSE connection mid-run does not cancel the run; reconnect replays from `Last-Event-ID`.

## M4. Approval and the booking path

Scope:
- `propose_*` validation in the loop, `prepare_proposal` (hold reuse, quote, `ActionCard`, fingerprint, pending approval, supersede), `await_approval`, `respond -> await_approval` routing.
- Approval routes (button grant with `pending_resumes`, decline), typed grant (action-verb grammar, `visible_card` with `message_id` captured at Send, conversation-position and render-ack checks), card reminder with `typed_grant_ok`, interrupt-aware input routing, lazy expiry sweep.
- `execute` (per-user consequential lock, preflight, guard transaction with duplicate re-checks, `ApprovalToken`, `ConsequentialClient`), `verify`, outcome templates, status line, `claim_guard` matched to records.
- Hostile policies `DirectBookPolicy`, `FalseClaimPolicy`, `ParaphraseClaimPolicy`.

Acceptance:
- S01 (with area given in the message; the memory variant lands in M12), S06, S10, S11, S12, S13, S18, S21, S27, S28, S29, S31, S54, S55, S62 pass.
- INV-01, INV-02, INV-04, INV-11, INV-12, INV-15, INV-16 hold on every scenario so far.
- import-linter contract is green with `executor.py` as the only importer of consequential calls.

## M5. Booking failure paths

Scope: `intent_attempts`, timeout and 5xx classification, status-first verify with backoff, the bounded same-key retry on `not_found` with the hold live (`booking.max_same_key_retries`, `booking.provider_dedupes_keys`, `action_intents.same_key_retries`), the hold-lapse wait rule as its fallback, a fresh hold and card after `not_found` on a lapsed hold, verify against the approved material, `cancelled_by_provider` branch, proactive re-hold near expiry, re-hold with fingerprint comparison and `hold_rebound`, `slot_taken` replan with a SystemNote, `PENDING_VERIFICATION` with the lapse time, world dedupe off mode as the degraded path (the harness derives `provider_dedupes_keys` from the scenario).

Acceptance:
- S19, S20, S22, S23, S24 (up to `PENDING_VERIFICATION`), S25, S50, S65, S68 and S69 (both up to `PENDING_VERIFICATION`), S71 pass; booking scenarios run with world dedupe both on and off.
- INV-03 holds everywhere (same key, same hold, a `not_found` on a live hold before every re-send, a status check after it, the count bound per mode, one world booking per key), checked in the trace and in the world `op_log`.
- SWEEP: the reference policy over 200 seeds, all invariants green.
- `errand-trace export --thread` exists, and the conversation-level trace of S22 is saved as the first draft of `docs/traces/timeout_after_success.jsonl`.

## M6. Restart and recovery

Scope: fault points (incl. `after_grant_commit` and `before_same_key_retry`), reconciler (startup scan before serving; non-terminal attempts, granted-unconsumed approvals and unfinished follow-up jobs; periodic scan of rows whose run lease expired; lease epochs and fenced attempt writes; original runs marked `recovered`; `ainvoke(None)` for other interrupted runs; `RUN_INTERRUPTED`), the critical section (Stop and general caps deferred once an intent exists; every exit with an open intent becomes `PENDING_VERIFICATION`), "Check now" and "Never mind" actions, user messages routed to `plan` during verification, subprocess restart harness against the HTTP world, recovery messages and notifications.

Acceptance:
- S36, S37, S38, S52, S53, S58, S59, S70, S72 pass; S24, S68 and S69 now complete through the reconciler.
- INV-16 and INV-18 hold on every scenario.
- A manual repro: kill the agent with `ERRAND_FAULT=after_world_book:exit` during a booking in the dev setup, restart, and see the verified message in the conversation.

## M7. Web app core and first live run

Scope:
- Chat page with streaming tokens and activity lines, `ClarifyCard`, `ProviderComparisonCard`, `ActionCard` (countdown, delta, stale state, 409 handling), `VerifyingCard`, `OutcomeCard`, trace panel (live tree, filters, model call detail), dev drawer (scenario, clock, faults), `/runs/[id]`.
- Types generated from OpenAPI.
- First live run with real Opus (the reader arrives in M10, so reviews are simply absent from the planner view until then): book a plumber end to end; record cassettes for S01 and S22.
- Measure live turn latency (p50 and p95 per planning turn) and set the live wall-clock limit from it.
- `errand chat`, the terminal fallback over the same API.

Acceptance:
- Playwright: approve to confirmed, double click books once, stale card shows "details changed" and refetches.
- The live run completes S01's flow in the browser with the trace panel updating; its cost and latency per turn are logged and the live deadline is set from them.
- `errand chat` completes S01's flow with only `uv` installed.
- S01 and S22 also pass in `replay` mode.

## M8. Conversation quality

Scope: `TimeWindowExpr` and `timewin.resolve`, `INVALIDATES` table and `invalidate` records, card invalidation on corrections, brief chips with provenance, value-bound evidence (section 10.7), out-of-coverage template, ranking with score parts and exclusions, reprompt for missing proposals, `NoEvidencePolicy`, `MatchingQuotePolicy` and `ReaskPolicy`.

Acceptance:
- S04, S05, S51, S60 pass; S03 also passes with `ReaskPolicy` (the guard catches it).
- `timewin` unit tests cover "this week", "next week", weekday names, "mornings", "not before 10 a.m.", week boundaries and the IST offset.

## M9. Cancel, reschedule and booking history

Scope: `list_bookings` resolution with booking refs and ambiguity, the pinned-provider path (`get_provider` admits a history provider to the shortlist; budget from `budget_ceiling` or waived with last price on the card), `propose_cancel`, `propose_reschedule` with candidates from the booked provider's slots, cancel retry with the same key, reschedule conflict handling, verification through `list_bookings`, intent indexes per target.

Acceptance:
- S07, S08 (without memory), S09, S26, S47, S48 pass.
- INV-01 and INV-02 hold for cancel and reschedule.

## M10. Untrusted output and the quarantined reader

Scope: reader with enum output, id checking, cache, `ReviewDigest`, `ProviderView`, sanitizer caps, zero-width stripping, injection badge in the UI, `review_signal` from unflagged star ratings only, `InjectionFollowerPolicy`.

Acceptance:
- S14 (including the rank comparison against clean reviews), S15, S16, S17 pass. INV-10 holds on every scenario.
- A live smoke run with the real reader on the seeded catalog flags every injection review and no clean ones (logged, not gated).

**Gate 2** after this milestone: memory and context design.

## M11. Memory

Scope: registry, store, explicit and inferred writes with value-bound evidence and the standing-preference cue, inference rules, confirm and dismiss, correction with supersede, forget with the cascade into brief fields, archived errands and `origin_brief_json`, redaction from evidence offsets and surface forms, blocklist, n-gram mask over every section and role, memory content stored by reference in messages, `propose_forget` chip, `list_memories` and `MemoryListCard`, memory API, `/memory` page, chat chips with Undo, `EagerForgetPolicy`.

Acceptance:
- S32, S33, S34, S35, S49, S61, S64 pass. INV-05 holds on every scenario.
- Playwright: forget a fact on `/memory`, then the next chat turn's context view shows it excluded with reason `forgotten`.

## M12. Context builder and budget

Scope: full sectioned builder, S3 sub-caps, memory relevance scoring and pre-fill with `memory_id`, ledger from archived errands and `known_bookings`, manifest with exclusion reasons, seed fixtures (60 memories with canaries in every role and in brief evidence, 240-message history), in-loop usage floor and loop compaction, `CONTEXT_TOO_LARGE`, `CONTEXT_EXHAUSTED`, live estimator calibration test with thinking-bearing fixtures.

Acceptance:
- S42, S43 pass. S01 (area from memory, zero questions) and S08 (with the memory variant) pass.
- INV-06 holds on every scenario, on the estimate in scripted mode and on reported usage in replay mode; the live calibration test shows the estimate is never below `count_tokens`; the eval notes show max actual input tokens.

## M13. Bounded execution, every exit path

Scope: remaining `RunOutcome` templates, `stop_reason` mapping (`max_tokens`, `refusal` to `MODEL_REFUSED`), cost cap, wall-clock deadline, outer backstop, node exception wrapper, `MODEL_UNAVAILABLE` retry, `ToolUnexpected` from malformed world responses, `LoopForeverPolicy`, `GarbageToolPolicy`.

Acceptance:
- S44, S45, S63 pass; a `GarbageToolPolicy` scenario ends with a clear outcome and no crash.
- INV-07 and INV-09 hold on every scenario.
- A test injects an exception into each node in turn and asserts a templated user message and a `run.end` every time.

**Gate 3** after this milestone: proactive follow-up policy and UX.

## M14. Proactive follow-up

Scope: watcher, `followup_jobs` with states and reconciler resume, card invalidation on provider cancel, policy (including the pending-cancel-card rule), follow-up conversations, replacement search within current preferences, "Rebook" card with `replaces_booking_id`, notifications table, user-level SSE stream, notification bell and toast.

Acceptance:
- S39, S40, S41, S56, S57, S66 pass. INV-17 holds on every scenario.
- Playwright: dev drawer "Provider cancels booking" produces a notification without any user message, and approving the rebook card books once.

## M15. Langfuse sink and infrastructure

Scope: `LangfuseSink` behind `make_sink()`, private tracer provider, live spans with explicit costs, pinned compose plus override and `.env.example`, `make langfuse-up`, health endpoint field.

Acceptance:
- With Docker up, one live booking shows in Langfuse with matching trace id, session and cost.
- With Docker down or keys absent, the app and the full eval suite run with no Langfuse import and no added latency (a test asserts `langfuse` is not in `sys.modules`).

## M16. Live-model hardening

Scope: record cassettes for the replay subset (at least one scenario per brief category, listed in architecture section 18.6), prompt tuning against them, measure the evidence-check reject rate and planner cost per turn, live smoke suite with invariant-only assertions.

Acceptance:
- The replay subset passes offline.
- The live smoke suite passes with all invariants; median cost per planning turn and evidence reject rate are recorded in the README.

## M17. Deliverables

Scope:
- `docs/DESIGN_NOTE.md` final pass (one page: state model, approval enforcement, idempotency and recovery, memory, observability, key trade-offs); the sections were filled milestone by milestone, so this is an edit, not a write. The annotated excerpt is cut from the real trace file and shows one planner `model.call` with usage and cost plus the timeout, status check and verified block.
- Final `docs/traces/timeout_after_success.jsonl`: the conversation-level export (`errand-trace export --thread`) of a live booking, from the first user message to `BOOKED_VERIFIED`, with the timeout forced through the dev drawer, which the note says plainly.
- `docs/BUGS.md` with the three strongest real bugs, each with its regression test.
- README: setup, `make dev`, `make evals`, architecture overview, limits, known limitations.
- Generated `docs/EVAL_TABLE.md` from a clean run.
- Demo script and rehearsal: one full booking with chat and trace side by side, then a live timeout-after-success via the dev drawer, optionally the injection badge.
- Coding-assistant transcripts: `scripts/export_transcripts.py` collects every session file for this project, including subagent sessions, into `docs/ai-transcripts/` (raw JSONL, one rendered Markdown file per session, `index.md` linking sessions to gates). The only edit is replacing the pasted API key with `[REDACTED-KEY]`, and the README says so.
- Rotate the Anthropic API key that was pasted in chat, and confirm no key appears in the repo history or in `docs/ai-transcripts/`.

Acceptance:
- A clean clone runs `make evals` offline and regenerates an identical table apart from the timestamp header.
- The design note fits on one page (`make check` enforces the budget).
- `docs/BUGS.md` lists three bugs, each with a repro commit that precedes its fix commit.

**Gate 4** after this milestone: final review before submission.
