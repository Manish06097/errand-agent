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
