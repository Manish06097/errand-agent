# Engineering journal

A short entry per milestone: what I set out to do, what I decided, what review caught, what is next.

## M0: repo scaffold and quality checks

**Goal.** Get a repo where the boring stuff is already enforced before any product code lands: a locked uv backend on Python 3.11, health-only `errand-api` and `errand-world` apps, a Next 16 web shell that reaches `/api/health` through the rewrite, `make dev` to run all three, and `make check` as the single gate.

**Decisions.**
- Exact pins and committed lockfiles on both sides (`uv.lock`, `pnpm-lock.yaml`), with `uv sync --locked` inside `make check` so drift fails loudly instead of quietly.
- The package skeleton exists now, empty, so the import-linter contracts and mypy overrides have real modules to bite on from day one. Layering is cheaper to hold than to retrofit.
- The repo rules (no em dashes, no tool attribution, no secrets, design-note word budgets) are code, not a checklist. The same guards run in `make check` and in pre-commit on both the pre-commit and commit-msg stages.
- `make dev` does a port preflight and supervises the three processes, so a stale server shows up as a clear error rather than a confusing half-working stack.
- Spike scripts and their raw output live in `spikes/`, copied by allowlist, with a README tying each finding to a raw file. Design choices that lean on library behaviour should be checkable.
- Deviation I accepted: `make evals` reports an empty suite as a pass while `backend/evals` holds only placeholders. Real scenarios arrive with the agent, and a red gate with nothing to evaluate would just teach everyone to ignore it. It goes strict as soon as the first scenario exists.

**What review caught.** BUG-001: after `make dev` under a coding agent, an untracked `web/AGENTS.md` appeared. Next 16's dev server writes a managed agent-rules file when it detects one, and that file contains em dashes, so `git add -A` tripped the guard and would otherwise have committed tool-generated instructions. I reproduced it end to end with a test that runs the real supervisor and checks `web/` afterwards (red on purpose, 8d14f54), fixed it with `agentRules: false` in `web/next.config.ts` (48c2d0d), re-ran the same repro green, and logged it in `docs/BUGS.md`.

**Next.** M1: the mock provider world with all 8 tools, the seeded failure model, and booking-timeout handling as signed off in GATE-01 (retry with the same idempotency key on the live hold, re-check status, then report).

## M1: the mock provider world

**Goal.** Build `errand.world` and the `WorldClient` that reaches it, strictly enough that the world's `op_log` and `bookings` can serve as ground truth for every later invariant: exactly on the brief's contract by default, every extra failure a named test-only fault, deterministic, and one engine behind both transports.

**Decisions.**
- One entry point, `World.handle(tool, payload_bytes) -> bytes`. The in-process transport calls it with JSON bytes and decodes the answer with the same strict decoder as HTTP, so malformed responses, key sets and datetime formats cannot drift between transports. A parity test runs the same script on both and compares the responses byte for byte.
- Every wire model is `extra="forbid"` and strict. Business answers are typed results with a closed error set per tool; timeouts, 5xx and anything that fails strict parsing raise `ToolTimeout`, `ToolTransient` and `ToolUnexpected`. The contract tests check key sets written out by hand, so a field added to a model on both sides still fails.
- Idempotency: the key is claimed `in_progress` in its own committed transaction, then the world sleeps, then it evaluates and commits. Waiting on an in-progress key is a per-key in-memory latch that is released at commit, before any post-commit timeout sleep, so no transaction or latch is ever held across a delay. The latch is process-local, so the HTTP world locks `<db>.lock` and refuses more than one worker.
- The world may only import `errand.clock`. That rules out reading `tools/timeouts.py`, so the HTTP client sends its timeout on every call and the server holds a timed-out connection open for exactly that plus the margin. Tests shrink the timeout to 0.3 to 0.6 s through settings.
- `op_log` rows are inserted on receipt and completed at the end, with both arrival and completion order, so the late-commit case reads directly off the log.
- `bangalore_v1` fixes provider ids and identities. The seed only decides which optional providers are listed and which hours are free, always through keyed draws. E4 is an unlisted Indiranagar electrician, so it answers `get_provider` but never shows up in search.
- The lead's four clarifications (no hold supersede, a `released` hold status, dedupe-off double booking, `in_progress` to `not_applied` on restart) are written into architecture 5.3 with the other M1 clarifications.

**What review caught.** No real bugs reached a test run. Before handing over, I ran the review mutations myself (no replay branch, no in-progress wait, `hold_expired` a second early, an extra `get_provider` field on both sides, a global `random.Random`). Each one turns contract tests red on both transports.

Round 2 (the lead's review):
- BUG-M1-1: `malformed: wrong_types` was a no-op for `search_providers` and `list_bookings`, because it only retyped top-level scalars and those bodies are one list. The named fault was silently off, the one thing this world must never be, and `test_faults` only covered `get_provider` and `book`, so nothing noticed. Repro over all 8 tools x 3 modes on both transports (e096307), plus the empty `{bookings: []}` body that even a depth-first retype would miss (f5150d8); fix in 0fcf85e; same repro green afterwards. The lesson: a fault is only proven when a test drives it through every tool it can be pointed at.
- BUG-M1-2: with dedupe off, a re-sent `book` on an applied key could rewrite the key's `operations` row to `not_applied` (or to the second booking), and a restart mid re-send did the same through the claim. Repros bfd6217 and 6deba81, fix 25644e0. To assert it on both transports the inspector now reads `operations` (`GET /dev/operations`, dev only).
- Also from the review: op_log rows are finished as `internal_error` (500) or `interrupted` when a call raises or is cancelled, and the late-commit test has wider margins (commit at 1.5 s, the retry on its own 3 s client). Every milestone commit now exists on `m/M1` in the brief's order, dependencies first, so the em-dash and secret guards, which `make check` runs over tracked files, now cover the M1 files.

Round 3 (the lead's review):
- BUG-M1-3: a consumed hold booked again under another key once its booking was cancelled or rescheduled away, because `book` only looked at the slot. The lead found it by driving the world the way a buggy agent would, re-booking from a stale hold id with a fresh key. Repro on both transports, dedupe on and off (87f1329), fix 18a9719, same repro green. The lesson: an object that is "used up" needs its own state checked, not just the shared resource it pointed at, because the resource can become free again.
- Docs and nits: the 5.6 duplicate-delivery row and the 5.5 fault comment now describe the engine mechanism, the enum example lists only real values, 5.3 says a late-commit row's `http_status` 200 is what the world sent (the trace says what the client saw, and the late-commit test pins the 200), and `HttpInspector` now wires itself as the `TrackingClock` advance hook, so a sim-mode agent sleep over HTTP moves the world, with a contract test on both transports.

**Sign-off.** Review passed after round 3. `make check` is green on the worktree ports (317 passed, 2 skipped by design: the late-commit wait is covered by the HTTP world test). Deviations I accepted on the way: the world refuses more than one HTTP worker because the in-progress latch is process-local, and the client sends its own timeout on every call so the world never imports the tools layer.

**Next.** M2 (store, tracer, model gateway) lands in parallel. M3 builds the ToolGateway on `WorldClient` and starts reading `fault_names()` for the eval table's `world` column.

## M2: foundations (store, tracer, model gateway)

**Goal.** Build the layer every later milestone stands on: `agent.sqlite` with every table and constraint from the design, the JSONL trace of record, and a model gateway whose cost, budget and replay behaviour can be proven offline.

**Decisions.**
- Every SQLite connection is opened with `isolation_level=None` and the pragmas are set per connection. All transactions go through `transaction_immediate`. A control test shows why: the same read-then-write in a deferred transaction fails with `database is locked` once another writer commits in between.
- One canonical request for every mode, built by ChatAnthropic's own payload builder minus the transport-only fields (`stream`, the beta list, `block_binding`). A test runs the real ChatAnthropic, SDK and HTTP client against a local fake transport and checks the canonical form is byte-identical to the request body, so scripted, replay and live hashes are the same hash.
- The planner loop goes through a `LoopSession` that compares the previous request plus response, byte for byte, with the next request before anything is sent. The response's wire form is taken from a position where something follows it, because ChatAnthropic right-strips the text of the last assistant turn only.
- The cost formula reads only `usage_metadata`. Totals are summed in `Decimal` from the costs as written, so `run.end` matches the `model.call` records exactly.
- The socket guard now lives in `backend/conftest.py`, records every violation (DNS lookups included), and fails the test after its call phase even if the code swallowed the error.

**Library facts found while building.** None of these is a bug in our code; each is now handled and tested.
- `bind_tools(strict=True)` passes a tool that is already an Anthropic dict through untouched, so `strict` is silently missing. `planner_bind_kwargs` sets it on every custom tool.
- On the beta endpoint the streamed `message_delta` usage has no TTL split, so live cache writes arrive as the generic `cache_creation` and are priced as 5 minute writes. Correct today (we only use 5 minute caching); wrong if a 1 hour TTL is ever used.
- The Anthropic SDK 2.6 runs on `httpx2`, not `httpx`, and sends the key as a bearer token.

**What review caught.**
- The milestone sat uncommitted in the worktree, so a clean checkout of the branch had none of it. It is now committed in small steps (conftest move first), and `make check` and `make evals` were run from a fresh clone.
- `ttft_ms` was timed at the first chunk of any kind, which with ChatAnthropic is the empty `message_start`. It is now timed at the first chunk with answer text or tool-use input; thinking deltas do not count. The scripted stream opens with an empty chunk too, so the offline tests meet the live shape.
- The SDK's default of 2 retries was implicit, hidden inside one `model.call`. It is now `ERRAND_PLANNER_MAX_RETRIES` (default 2), asserted against the client and the retry-count header. M13 picks the final value against the deadline.
- `LoopSession` fell back to checking only the sent request when the response could not be placed. That case is real: ChatAnthropic drops an assistant turn with empty content from any request where it is not last. The empty turn is still returned and traced, but the next `call()` now raises `LoopPrefixViolation`.
- `trace` imported `llm.pricing` while `llm` imported `trace`. `Usage` moved to `errand/usage.py`, the gateway prices the call and hands the tracer the cost, and an import-linter layers contract pins `llm` above `trace` above `usage`.

**What broke.** BUG-M2-1: filling the Observability section of the design note turned `make check` red, because the M0 design-note tests used the real note as an empty template. I reproduced it with a test that runs those tests against a copy of the repo whose note has every section filled (red on purpose, 8273ef1), fixed the tests to use a skeleton of the note (e533650), re-ran the same repro green, and logged it in `docs/BUGS.md`.

Second review found two more, each reproduced through a real Tracer with JSONL and stream sinks, committed red, fixed, re-run green and logged:
- BUG-M2-2: a landline in a `phone` field ("080 4123 4567") or in free text reached the trace in the clear, and so did a pincode after a city name. Phones were masked only by a mobile and +country regex. Phone-like keys are now masked by name, and free text also masks landlines and city pincodes (056c990, 4b6042e).
- BUG-M2-3: a record rejected by schema v1 still used up a seq number and a span id, so the trace read seq [1, 3]. The counters now advance only after validation (7543bee, 984c93e).

**Next.** M3: graph skeleton, API, RunManager and the eval harness on top of these pieces. For M13: SDK retries happen inside one `model.call`, so retry time is in its latency but not shown on its own; trace it when the retry and deadline caps land.

## M3: graph skeleton, API and eval harness

**Goal.** The first end-to-end path: a user message goes through FastAPI, the RunManager, the LangGraph graph (`ingress -> plan -> respond`), the planner loop and the world, and comes back over SSE. On the same path, the harness that proves it: the scenario DSL, SimUser, the reference policy, five invariants and the generated eval table.

**Decisions.**
- `ThreadState` is the full 7.1 tree from day one, including fields later milestones fill, so the checkpoint shape does not change under them. Facts are concrete classes (`ServiceFact`, `AreaFact`, ...): I checked the lead's doubt first, and a parametrised `Fact[Service]` comes back from a strict-msgpack checkpoint as a plain dict, silently, which a later validation would paper over. A fresh-process round-trip test walks every restored value's type.
- `LANGGRAPH_STRICT_MSGPACK` is set in `errand/__init__.py`, not in `errand.agent`: ruff's import sorting put `langgraph` above `errand.agent` in `api/app.py`, which would have loaded langgraph with strict mode off. The root package always runs first. `build_graph` refuses to compile if strict mode did not take.
- `TimeWindowExpr` has no `days` (the lead's call); `update_brief`'s window is `{phrase, part_of_day}` only in M3. `not_before`/`not_after` stay on the domain type for M8 but left the tool schema, both because M3's closed set does not include them and because the tool list sat 40 tokens under its 2,000-token section cap. It is at 1,785 now; M4's `propose_*` tools will need the same discipline.
- One `RunCounters` type (`domain/counters.py`), shared by both gateways for a run and copied into state when `plan` ends. The gateway's old dataclass is gone.
- `context.built` is one record per model call: the gateway merges the builder's manifest into its own, rather than two manifests that could disagree.
- Ids: `c_0001`/`r_0001` in sim mode, counters started past the highest id agent.sqlite holds (not its row count, BUG-M3-2), so a restart never reuses one; uuid7 live. A run id is minted only once the thread lease is taken. Sim ids assume one API process per data directory. Message and errand ids are derived from the run id, so no node mints anything a re-run would mint differently.
- `respond` upserts on `(run_id, kind)` (migration 0002, a partial unique index over assistant rows; the user's row carries the run id it started, which is how a dedupe hit finds its run).
- Leases run on real time, never the simulated clock: they measure whether the process is alive, and a scenario that advances simulated time by 11 minutes must not expire a run's lease mid-run.
- The tool gateway caps each call's timeout by the run time left through a context variable the transports read, and learns the world's `op_log` seq the same way (new `X-World-Seq` header over HTTP, a `seq` on the engine's response in process), so `tool.call.world_seq` points at the world's own record.
- A planner call to a world tool that is not a planner tool (`book`) is forwarded to the ToolGateway as the planner, so its allowlist is the control that refuses it, not just a branch in `plan`.
- `make dev` gives the API `ERRAND_WORLD_URL` and puts `backend/` on its `PYTHONPATH`, so scripted mode loads `evals.policies.reference` by name (`ERRAND_SCRIPTED_POLICY`); nothing in `errand` imports `evals`.
- The report plugin writes `docs/EVAL_TABLE.md` only when `make evals` asks (`--write-report`), so a filtered run, or the make-target tests, never overwrite the committed table.

**Deviations from the brief.**
- The `ask_user` guard treats "the user's reply after the first ask did not fill it" as "the field is unfilled now". `update_brief`'s `clear` is live in M3, so a retraction is reachable today. The lead's ruling (round 2): a retraction is not the user giving the value, so after an applied clear the field's one re-ask with options is legal and still counts toward the two. INV-13 now un-fills a field on an applied clear (BUG-M3-3), so the guard and the invariant agree. M8 keeps value-bound corrections and `INVALIDATES`.
- An illegal transition is recorded (`reason: "rejected: ..."`) before `IllegalTransition` is raised, so INV-08 sees the attempt as well as the run failing.
- `update_brief` is all or nothing: one rejected value rejects the patch, as 10.7 describes, rather than applying the good fields.

**Verified.** `make check` and `make evals` green on 9100/9101/9102; two `make evals` runs differ only in the timestamp. I applied each review mutation by hand and each turned something red: forwarding search args (search-args test), no ask guard (S67, INV-13), `book` in the tool list (INV-11, in every scenario), an illegal transition (INV-08), skipping `run.end` on an exception (lifecycle test), no dedupe or dedupe after the 409 (S30, admission tests), cancelling on SSE disconnect (disconnect test), reviews back in `ProviderView` (INV-10, view test), a tolerant SimUser (harness test), and a generic `Fact` (round-trip test). Manual run on `make dev`: curl create, post, `-N` events, replay with `Last-Event-ID`, and a second API process continuing S03's turn 3 from the checkpoint over the HTTP world.

**What broke.** BUG-M3-1, found by the lead in review: `make evals` was not strict. pytest turns an empty `parametrize` list into one skipped test and exits 0, not 5, so with the YAML renamed to `.yml` the suite went green and the report plugin kept the old table. I had deleted the M0 tests that would have caught it instead of adapting them. Repro on a copy of the real evals tree (cd29408), fix (8ced0c1): an empty or misnamed catalogue is a collection error, pytest fails any empty parametrize at collection, and the plugin refuses an empty or incomplete table.

**Review round 2.**
- Committed in dependency order (domain, llm, transitions, tools, brief ops, store, context, graph, API, harness, tests, scenarios, the bug pair, docs, table), each built in a clean clone so the pre-commit hooks checked exactly that commit.
- INV-10 now reads every review in world.sqlite, plus any served, so S46 and S67, which never open a provider, check the whole catalogue too; the count is in the table's notes.
- `ask_user`'s question and options are cleaned like world text and bounded in code (6 options, 40 characters each). The bound lives in the Pydantic model and the tool description, not the schema, because strict mode rejects `maxItems` and `maxLength`. The reference policy now sends at most 6 quick replies, round-robin over the missing fields.
- `get_provider` honours only a shortlist built for the current brief version; a hostile two-turn policy shows the stale open never reaches the op_log.
- `LeaseKeeper.release` waits on the renew task without re-raising, so the release UPDATE always runs, and a failed renewal is logged and retried.
- The import-linter contract waives two edges only (`tools.runtime` and `tools.client` to `world.engine`); adding `import errand.world.store` to `tools/gateway.py` breaks it for both `errand.agent` and `errand.api`.

**Review round 3.**
- BUG-M3-2, found by the lead: a restarted sim-mode API reused a run id. The counters were seeded from COUNT(*), and an admission refused at the lease (a crashed worker's stale lease) had already minted its run id, so the table fell one row behind the highest id and the next process minted an existing one: a 500 on `messages.message_id`. Repro through the API with a stale lease and a restart (df6fdd6), fix (33a4b3e): seed from the highest numeric suffix, and mint the run id after the lease, so a refused lease burns nothing. The lease holder now names the admission, not the run (made unique across restarts by BUG-M3-4).
- BUG-M3-3, found by the lead: INV-13 flagged the guard's legal re-ask after a retraction. Repro with a planner that clears a retracted area, both while GATHERING and while COMPARING (b39b2ac), fix (5d049fd): an applied clear resets INV-13's "filled since the first ask". The COMPARING flow also puts COMPARING -> GATHERING (`required_field_cleared`) under test. Negatives: a field filled and never cleared, or cleared by a rejected patch, still fails INV-13 when re-asked.
- The LIMIT_REACHED notice no longer claims content it does not show: it names the brief's filled search fields ("So far I have: plumber, Indiranagar, ...") and leaves the sentence out when there are none.
- A search that finds nobody ends REPLIED with the model's answer instead of PRESENTED_OPTIONS with an empty card. Handoff to M8: a code-written no-fit template (what to widen: window, budget, area) replaces the model's text there, and the errand stays COMPARING with an empty shortlist until then.
- BUG-M3-4, found by the lead: the admission-named holder `api-<pid>:a<n>` repeated after a restart with the same pid, so the restarted app took a dead run's live lease with a 202 and kept its epoch, which M6 fences on. Repro through the API with the BUG-M3-2 harness, restarting in the same process (cd543a7), fix (bbc31fa): the holder is `api-<pid>-<token>:a<n>` with a random 8-byte token per RunManager. It lives only in the leases table, never in a trace, a model request or the eval table, so determinism is untouched.

**Sign-off.** I set out to get a thin but real vertical slice: the graph skeleton, the API with its SSE stream and run lifecycle, and an eval harness strict enough to trust. Review took three rounds and caught four real bugs (BUG-M3-1 to BUG-M3-4), each reproduced through the product surface before the fix and re-run after. The deviations above are accepted as they stand; the no-fit template moves to M8. `make check` is green on 9100/9101/9102 at sign-off.

**Next.** M4: `await_approval` lands on the routing check that already exists, `propose_*` on the fixed tool list, and the run lifecycle as built here.
