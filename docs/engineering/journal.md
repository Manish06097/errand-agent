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
