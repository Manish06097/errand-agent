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

**Next.** M2 (store, tracer, model gateway) lands in parallel. M3 builds the ToolGateway on `WorldClient` and starts reading `fault_names()` for the eval table's `world` column.
