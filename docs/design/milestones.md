# Build plan

Each milestone is small enough to build and test in one focused session, ends with a green `make check` and `make evals`, and is committed on its own. Scenario ids (S01 to S72, SWEEP) and invariant ids (INV-01 to INV-20) refer to `docs/design/architecture.md` section 18.

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
| Gate 2 | after M10, before N2 (signed off 2026-11-09) | memory registry, forget semantics, relevance scoring, context budget split, seed data |
| Gate 3 | inside N3, after bounded execution and before follow-up (one page) | follow-up notify policy, replacement rules, card and notification UX |
| Gate 4 | after N4 | design note, eval table, bugs write-up, trace excerpt, demo script before submission |

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

Scope: `TimeWindowExpr` and `timewin.resolve`, `INVALIDATES` table and `invalidate` records, card invalidation on corrections, brief chips with provenance, value-bound evidence (section 10.7), out-of-coverage template, the "Why this order" rendering of the score parts and the exclusion UX (the ranking function itself landed in M10), reprompt for missing proposals, `NoEvidencePolicy`, `MatchingQuotePolicy` and `ReaskPolicy`.
- The gate 2 budget split (architecture 13.1), applied here because `not_before`/`not_after` take `update_brief` from 984 to about 1,080 and push the tool list past its old cap: S2 3,800 with the 400-token preamble inside it, S4 600, S6 1,000, S8 2,300 as a floor, slack 200. A config test fails if the caps do not sum to 12,000, and a test renders the bound tool list and fails over S2's cap. `INVALIDATES` lists, per brief field, whether a change invalidates a pending card; M11's forget and correct reuse it.

Acceptance:
- S04, S05, S51, S60 pass; S03 also passes with `ReaskPolicy` (the guard catches it).
- The cap tests pass with the widened `update_brief`, and the eval notes show every scenario's maximum estimate under 12,000.
- `timewin` unit tests cover "this week", "next week", weekday names, "mornings", "not before 10 a.m.", week boundaries and the IST offset.

## M10. Untrusted output and the quarantined reader

Scope: reader with enum output, id checking, cache, `ReviewDigest`, `ProviderView`, sanitizer caps, zero-width stripping, injection badge in the UI, `review_signal` from unflagged star ratings only, `InjectionFollowerPolicy`.

Acceptance:
- S14 (including the rank comparison against clean reviews), S15, S16, S17 pass. INV-10 holds on every scenario.
- A live smoke run with the real reader on the seeded catalog flags every injection review and no clean ones (logged, not gated).

**Gate 2** after this milestone: memory and context design.

## M15. Langfuse sink and infrastructure

Scope: `LangfuseSink` behind `make_sink()`, private tracer provider, live spans with explicit costs, pinned compose plus override and `.env.example`, `make langfuse-up`, health endpoint field.

Acceptance:
- With Docker up, one live booking shows in Langfuse with matching trace id, session and cost.
- With Docker down or keys absent, the app and the full eval suite run with no Langfuse import and no added latency (a test asserts `langfuse` is not in `sys.modules`).

---

# Remaining plan

## Scope trim (2026-11-09)

This is a take-home, so from here the plan builds what the brief (`ASSIGNMENT.pdf`) asks for and grades, at the same quality bar, and stops there. M0 to M5, M7, M8, M10 and M15 above are done or in flight and keep their entries. The eight milestones that had not started (M6, M9, M11, M12, M13, M14, M16, M17) become four, N1 to N4, each tied to brief requirements. Where an entry above names an old milestone, read M6 and M9 as N1, M11 and M12 as N2, M13 and M14 as N3, M16 and M17 as N4.

Nothing the brief requires is dropped: every row of its tool table, every section 3 constraint and every section 5 deliverable has a home above or in N1 to N4. Gate 2's decisions stand as signed. Everything that is built still gets its end-to-end proof.

Cut, and why:
- **From M6:** fault points beyond the three that restarts need, lease-epoch fencing of attempt writes, the periodic scan of expired-lease runs, the `ainvoke(None)` resume of interrupted runs, resuming a granted approval within a grace window, "Never mind" and plan routing during verification. Correctness needs a startup reconciler, verify before any claim and no double booking across a restart. It does not need the rest.
- **From M9:** the choice card for an ambiguous "last electrician". A plain question listing the matches does the job.
- **From M13:** the server-side refusal fallback to a second model, the per-run cost cap and the `GarbageToolPolicy` scenario. The brief asks for limits on turns, tool calls and wall-clock time. `refusal` still maps to `MODEL_REFUSED`, and S63 covers unexpected tool output.
- **From M14:** the pending-cancel-card rule, the race between the follow-up and a same-chat replacement, and the crash in the middle of a follow-up. The brief asks to detect the cancel, decide whether and how to notify, replace within preferences and get fresh approval. S39 to S41 prove that.
- **From M15 and M16:** no more Langfuse work (the A5 re-run and the Docker memory change are dropped, and the demo shows our own trace panel). No cassette per brief category, no prompt-tuning pass and no evidence-reject-rate study. Evals run on the scripted model, which the brief allows; a small replay subset and a live smoke stay.
- **Scenarios:** the brief asks for at least 20 and 33 run today. M5 and M8 add 15, N1 to N3 add 23, so the catalogue ends at 71 instead of about 90. Cut from the plan: S09, S16b, S32b, S33, S34b, S35b, S38, S42b, S42c, S53, S56, S57, S58, S59, S61, S66, S70, S72 and the `GarbageToolPolicy` scenario. Each proves something the brief does not ask for, or repeats what a kept scenario or an every-scenario invariant already checks (INV-05, INV-16, INV-18, INV-19).

## N1. Restart, cancel and reschedule

Brief: no duplicate bookings across process restarts; cancel and reschedule only on explicit approval; the `cancel` 503 and `reschedule` `slot_conflict` behaviours; examples 2 and 3.

Scope:
- Startup reconciler (the core of M6), run before the API serves. It verifies every open intent by its key through `get_booking_status`. On `confirmed` it records the booking and tells the user. On `not_found` with the hold live it re-sends once with the same key (Gate 1 Q1); past expiry `not_found` is definitive and ends `ACTION_FAILED` with a fresh quote. While status is unavailable the intent stays `PENDING_VERIFICATION` with the lapse time. The same pass runs as a tick that the evals, the dev drawer and a "Check now" button on the verifying card can trigger.
- Other rows found at startup: a granted but unconsumed approval is expired with a message and a fresh card, never resumed. A run with no open intent ends `RUN_INTERRUPTED` with a message.
- The critical section: once an intent exists, Stop and the general caps wait for verify, and any exit with an open intent ends `PENDING_VERIFICATION`.
- Fault points `after_intent_write`, `after_world_book` and `after_grant_commit` through `ERRAND_FAULT`, and a subprocess harness that kills and restarts the agent against the HTTP world.
- Cancel, reschedule and history (M9 as written): `list_bookings` resolution with booking refs, ambiguity answered with a question listing the matches, the pinned-provider path for "same electrician as last time", `propose_cancel`, `propose_reschedule` with candidates from the booked provider's slots, cancel retry with the same key, reschedule conflict handling, verification through `list_bookings`, intent indexes per target. Both tools fit inside S2's 3,800 tokens.

Acceptance:
- S07, S08 (without memory), S26, S36, S37, S47, S48 and S52 pass. S24, S68 and S69 now complete through the reconciler tick.
- S37 is a real restart: the harness kills the agent at `after_world_book`, starts it again, and the conversation shows the verified message, with exactly 1 world booking.
- INV-01 and INV-02 hold for cancel and reschedule; INV-16 and INV-18 hold on every scenario.
- The S2 cap test passes with both new tools bound.

## N2. Memory and context

Brief: remember stable preferences across sessions; inspect, correct and forget; explicit versus inferred; at least 50 seeded memories and a long prior conversation; a fixed token budget; only relevant, current preferences reach the model; the context of every turn logged and proven in tests.

Scope: M11 and M12 as signed in `GATE-02.md`, decisions 1 to 24 unchanged.
- Memory: the registry in code, migration 0004 and the HMAC key file; explicit writes only from a cue-bearing span of the user's message; inferred rows only from the two code rules over bookings, pending until Keep; memory text treated as data; correct as a new version in one transaction; forget final in `agent.sqlite`, with redaction, the cascade, the open-errand scrub and the `memory_epoch` rebuild mid-run; bookings stay; mask on read for traces; pre-fill with `prov.memory_id`; `/memory` page, chat chips with Undo, `MemoryListCard`, the memory API.
- Context: the sectioned builder at the 12,000-token caps M8 set, relevance as a gate with the score only ordering, shadowing and ties, the ledger, the shed order, loop compaction, `CONTEXT_TOO_LARGE` and `CONTEXT_EXHAUSTED`, the manifest on every `context.built` and the trace panel's Context view.
- Seeds through the real code paths: `memory_demo` (eight rows) for `make dev` and the demo, `memory_rich_v1` (60 rows, 240 messages) for the evals.
- Simplified where the gate left room: the selection order (shadowing, ties, `over_budget` drop order) is proven by unit tests on the pure selection function instead of S42b and S42c, and only S34 and S64 also run on `memory_rich_v1`.

Acceptance:
- S32, S34, S35, S42, S42d, S43, S49, S49b and S64 pass, plus S16's memory leg, S60's "this time" leg, S01 with the area from memory (zero questions) and S08's memory variant. S42 shows the budget binding as signed: `turns_dropped` at least 1, a ledger overflow line, S3 sub-capped and a request over 9,000 estimated tokens. S34 and S64 also pass on `memory_rich_v1`.
- INV-05, INV-06, INV-19 and INV-20 hold on every scenario.
- After a forget, a dump of `agent.sqlite` holds no plaintext of the value; one trace read through `trace/reader.py` is masked and the raw file is not.
- Playwright: forget a fact on `/memory`, and the next turn's Context view shows it excluded as `forgotten`.
- The live calibration test shows the estimate is never below `count_tokens`.

## N3. Bounded execution and proactive follow-up

Brief: hard limits on model turns, tool calls and wall-clock time; a clear outcome on every exit path; exhausted retries and unexpected tool errors handled; a provider cancel detected without a user prompt, a decision on whether and how to notify, a replacement within known preferences and fresh approval before rebooking.

Scope, part A (bounded execution): the remaining `RunOutcome` templates, `stop_reason` mapping (`max_tokens`, `refusal` to `MODEL_REFUSED`), wall-clock deadline, outer backstop, node exception wrapper, `MODEL_UNAVAILABLE` retry, `ToolUnexpected` from malformed world responses, `LoopForeverPolicy`.

**Gate 3** between part A and part B, one page in `docs/design/GATE-03.md`: I sign the follow-up policy (which cancels notify and how, and what `contact_window` changes), the replacement rules (current preferences only, never widened without me) and the card and notification UX.

Scope, part B (proactive follow-up): watcher for provider-cancel events, `followup_jobs` with states and the startup reconciler resuming unfinished jobs, pending cards on the cancelled booking invalidated, the signed policy, a follow-up in its own conversation (`kind=followup`, one `proactive` line in the booking's conversation, per Gate 3 decision 4), replacement search within current preferences, a "Rebook" card with `replaces_booking_id` that needs a fresh approval, notifications table, user-level SSE stream and a bell.

Acceptance:
- S44, S45 and S63 pass; S39, S40 and S41 pass.
- INV-07, INV-09 and INV-17 hold on every scenario.
- A test injects an exception into each node in turn and asserts a templated user message and one `run.end` every time; with an open intent the run ends `PENDING_VERIFICATION` and the reconciler resolves it.
- Playwright: the dev drawer's "Provider cancels booking" produces a notification with no user message, and approving the rebook card books once.

## N4. Deliverables

Brief: section 5 (repo and README, 1-page design note, generated eval table, three real bugs, one timeout-after-success trace with an annotated excerpt, a 3 to 5 minute demo, the coding-assistant transcripts), and a real LLM in the loop with offline evals.

Scope:
- Live check (the useful part of M16): replay cassettes for S01 and S22 (recorded in M7) plus S07, S14 and S39, run offline in `make evals`; a live smoke over the same five with invariant-only assertions; median cost and latency per planning turn in the README, read from those traces.
- `docs/DESIGN_NOTE.md` final pass (one page: state model, approval enforcement, idempotency and recovery, memory, observability, key trade-offs). The sections were filled milestone by milestone, so this is an edit. The annotated excerpt is cut from the real trace and shows one planner `model.call` with usage and cost, then the timeout, the status check and the verified block.
- Final `docs/traces/timeout_after_success.jsonl`: the conversation-level export of a live booking from the first message to `BOOKED_VERIFIED`, with the timeout forced through the dev drawer, which the note says plainly.
- Three real bugs written up from `docs/BUGS.md` (what broke, why, the fix, the regression test), with the full log kept below them. Shortlist: BUG-M1-3, BUG-M3-1, BUG-M4-2; Gate 4 picks.
- Generated `docs/EVAL_TABLE.md` from a clean run.
- README: setup, `make dev`, `make evals`, architecture overview, limits, known limitations, the retention statement.
- Demo script and rehearsal: one full booking with chat and trace side by side, then a live timeout-after-success through the dev drawer, optionally the injection badge. I record the video.
- Coding-assistant transcripts: `scripts/export_transcripts.py` collects every session file for this project, subagent sessions included, into `docs/ai-transcripts/` (raw JSONL, one Markdown file per session, `index.md` linking sessions to gates). The only edit is replacing the pasted API key with `[REDACTED-KEY]`, and the README says so.
- Rotate the Anthropic API key that was pasted in chat, and confirm no key is in the repo history or in `docs/ai-transcripts/`.

Acceptance:
- A clean clone runs `make evals` offline, replay subset included, and regenerates an identical table apart from the timestamp header.
- The live smoke passes with all invariants.
- The design note fits on one page (`make check` enforces the budget).
- The bugs write-up names three bugs, each with a repro commit that precedes its fix commit.

**Gate 4** after this milestone: final review before submission.
