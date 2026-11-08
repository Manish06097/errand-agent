# Run from the repo root. Python tools come from backend/uv.lock via uv, the web app
# from web/pnpm-lock.yaml via pnpm.
# `uv sync --locked` fails when uv.lock no longer matches the pins in pyproject.toml;
# `--frozen` would quietly install the lock's versions instead. Once the sync has
# passed, the later `uv run --frozen` calls can skip re-checking.

UV := uv run --frozen --project backend
PNPM := pnpm -C web

export NEXT_TELEMETRY_DISABLED := 1

.PHONY: help setup dev check evals e2e record langfuse-up langfuse-down

help:
	@echo "make setup        install backend and web dependencies and the git hooks"
	@echo "make dev          world, API and web together (ports: ERRAND_WORLD_PORT,"
	@echo "                  ERRAND_API_PORT, ERRAND_WEB_PORT; defaults 8765, 8000, 3200)"
	@echo "make check        every lint, type, hygiene and test gate"
	@echo "make evals        offline eval suite; regenerates docs/EVAL_TABLE.md"
	@echo "make langfuse-up  self-hosted Langfuse on ERRAND_LANGFUSE_PORT (default 3100);"
	@echo "                  make dev then sends traces to it. make langfuse-down stops it"
	@echo "make e2e | record   not built yet"

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
	cd backend && uv run --frozen pytest tests
	$(PNPM) typecheck
	$(PNPM) lint

evals:
	uv sync --locked --project backend
	$(UV) python scripts/run_evals.py --write-report

e2e:
	@echo "make e2e: not built yet, lands in M7" >&2; exit 2

record:
	@echo "make record: not built yet, lands in M7" >&2; exit 2

langfuse-up:
	$(UV) python scripts/langfuse_stack.py up

langfuse-down:
	$(UV) python scripts/langfuse_stack.py down
