# Run from the repo root. Python tools come from backend/uv.lock via uv, the web app
# from web/pnpm-lock.yaml via pnpm.
# `uv sync --locked` fails when uv.lock no longer matches the pins in pyproject.toml;
# `--frozen` would quietly install the lock's versions instead. Once the sync has
# passed, the later `uv run --frozen` calls can skip re-checking.

UV := uv run --frozen --project backend
PNPM := pnpm -C web

export NEXT_TELEMETRY_DISABLED := 1

# The test suite runs in two pytest steps (make check), and together they run every
# test exactly once:
#   1. `-m "not slow"` under pytest-xdist: -n 4 --dist loadscope. Measured on an 11-core
#      machine, -n 4, 6, 8, 10 and auto all land at 10 to 12 s: the floor is the longest
#      module, not the worker count, so four workers cost the least CPU and memory and
#      leave the machine quiet for step 2. loadscope keeps a module on one worker, which
#      pays each module's session-cached scenario runs (tests/evals) once per module.
#   2. `-m slow`: the tests that start subprocesses or real servers or shell out to make
#      and git (marker `slow`). Their time is process start-up, not CPU, so they share
#      the machine well: -n 2, 3 and 4 with --dist loadfile took 64, 46 and 41 s against
#      121 s serial. -n 3 --dist loadfile (a file stays on one worker, so its fixtures
#      and temp repos are never split); each worker has its own port block
#      (backend/tests/conftest.py). Serial (`-p no:xdist`) is 135 s with step 1, over
#      the 120 s goal.
# -n is not in addopts, so make evals and any other pytest call run as before.

.PHONY: help setup dev check evals e2e record smoke langfuse-up langfuse-down

help:
	@echo "make setup        install backend and web dependencies and the git hooks"
	@echo "make dev          world, API and web together (ports: ERRAND_WORLD_PORT,"
	@echo "                  ERRAND_API_PORT, ERRAND_WEB_PORT; defaults 8765, 8000, 3200)"
	@echo "make check        every lint, type, hygiene and test gate"
	@echo "make evals        offline eval suite; regenerates docs/EVAL_TABLE.md"
	@echo "make langfuse-up  self-hosted Langfuse on ERRAND_LANGFUSE_PORT (default 3100);"
	@echo "                  make dev then sends traces to it. make langfuse-down stops it"
	@echo "make e2e          Playwright: brings up its own stack (scripted planner, fresh data"
	@echo "                  dir, ERRAND_DEV=1), runs web/e2e and tears it down"
	@echo "make record SCENARIO=S01   record the scenario's cassette with the real key (prints cost)"
	@echo "make smoke        live smoke: S01, S07, S14, S22 and S39 on the real models, invariants"
	@echo "                  only, cost per scenario printed, total under 1.50 USD (needs the key)"

setup:
	uv sync --locked --project backend
	$(PNPM) install --frozen-lockfile
	$(UV) pre-commit install

dev:
	uv sync --locked --project backend
	$(PNPM) install --frozen-lockfile
	$(UV) python scripts/dev.py

check:
	uv sync --locked --project backend
	$(PNPM) install --frozen-lockfile
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy --config-file backend/pyproject.toml
	$(UV) lint-imports --config backend/pyproject.toml
	$(UV) python scripts/check_em_dash.py --tracked
	$(UV) python scripts/check_attribution.py --history
	$(UV) python scripts/secret_scan.py --tracked
	$(UV) python scripts/check_design_note.py
	cd backend && uv run --frozen pytest tests -m "not slow" -n 4 --dist loadscope
	cd backend && uv run --frozen pytest tests -m slow -n 3 --dist loadfile
	$(PNPM) typecheck
	$(PNPM) lint
	$(UV) python scripts/gen_api_types.py --check

evals:
	uv sync --locked --project backend
	$(UV) python scripts/run_evals.py --write-report

e2e:
	uv sync --locked --project backend
	$(PNPM) install --frozen-lockfile
	$(PNPM) exec playwright install chromium
	$(PNPM) e2e

record:
	@test -n "$(SCENARIO)" || { echo "usage: make record SCENARIO=S01" >&2; exit 2; }
	uv sync --locked --project backend
	$(UV) python scripts/record_cassette.py $(SCENARIO)

# The live smoke is never part of `make check` or `make evals`: it spends real money.
smoke:
	uv sync --locked --project backend
	cd backend && ERRAND_LIVE=1 uv run --frozen pytest tests/llm/test_live_scenario_smoke.py -m live -s -p no:xdist

langfuse-up:
	$(UV) python scripts/langfuse_stack.py up

langfuse-down:
	$(UV) python scripts/langfuse_stack.py down
