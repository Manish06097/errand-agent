# Errand agent

An agent that arranges home-service errands (find a provider, book, cancel or
reschedule) and asks before it acts. It is built milestone by milestone
(`docs/design/milestones.md`). This README describes only what exists now, which is
milestone M0: the repository scaffold and its hygiene gates. There is no agent, mock
world or chat UI yet.

## What is here

| Path | Contents |
|---|---|
| `backend/` | uv project, Python 3.11, exact pins in `pyproject.toml` and `uv.lock`. Two FastAPI services that only report health: `errand-api` (`GET /api/health`) and `errand-world` (`GET /health`). Empty packages for the modules the import-linter contracts name. |
| `web/` | Next.js 16 and React 19 shell (Tailwind v4, pnpm). Its page calls `/api/health` through the Next rewrite to the API and shows the result. |
| `scripts/` | `dev.py` (the `make dev` supervisor), the hygiene guards, `check_design_note.py`, `run_evals.py`. Standard library only. |
| `spikes/` | The pre-design experiments the design cites, with raw outputs. See `spikes/README.md`. |
| `docs/` | Architecture, build plan and gate records (`docs/design/`), ADRs (`docs/adr/`), the design-note skeleton (`docs/DESIGN_NOTE.md`) and the bug log (`docs/BUGS.md`). |

## Requirements

uv (it fetches Python 3.11 if needed), Node 20.9 or newer, pnpm 10.24 or newer (10.x),
git and make. macOS or Linux.

## Memory and retention

The agent remembers stable preferences you state ("I live in Indiranagar", "never before 10"), keeps the ones it guessed apart until you click Keep, and lets you see, correct and forget each one. Forgetting is final in `agent.sqlite`: the value is overwritten in every message that held it and then removed from its row. Three things outlive it on purpose. The JSONL traces under `data/`, older checkpoints of a conversation and Langfuse (when it is on) keep a forgotten value until you delete them; the app masks it on every read it serves but does not rewrite those files. The HMAC hashes that drive the masking stop the value being shown again, not someone who holds both `data/memory_hmac.key` and the database. And bookings stay: forgetting a preferred provider does not erase the booking history.

## Commands

| Command | What it does |
|---|---|
| `make setup` | Installs backend and web dependencies from the lockfiles and the git hooks. |
| `make dev` | Starts the world (8765), the API (8000) and the web app (3200) with prefixed logs. Stops all three if one exits or on Ctrl-C. |
| `make check` | `uv sync --locked` (fails if `uv.lock` disagrees with the pins in `pyproject.toml`), ruff, ruff format, mypy, import-linter, the em-dash, attribution and secret guards, the design-note budget, `pytest backend/tests` in two steps (the fast tests under pytest-xdist, then the `slow` ones that start subprocesses or servers, three workers, one file each), web typecheck and lint. |
| `make evals` | The offline eval suite. There are no scenarios yet, so it prints `0 scenarios`. If pytest collects nothing while `backend/evals` holds anything besides placeholders, it fails instead. |
| `make e2e`, `make record` | Not built yet; they say so and exit 2. |
| `make langfuse-up`, `make langfuse-down` | Optional self-hosted Langfuse on 127.0.0.1:3100 (`infra/langfuse`, about 4 GiB of Docker memory). `make dev` then sends each run's spans there; nothing else needs it. |

Ports come from `ERRAND_WORLD_PORT`, `ERRAND_API_PORT` and `ERRAND_WEB_PORT`. If one is
taken, `make dev` stops before starting anything and names the variable to set. The
web app's `/api` rewrite follows the API port, or `ERRAND_API_URL` if set:

```sh
ERRAND_API_PORT=8100 make dev
curl localhost:3200/api/health    # {"status":"ok","service":"errand-api"}
```

The tests bind port 0 wherever they can. A test that needs a fixed port takes it from
`worker_port(slot)` in `backend/tests/conftest.py`: `ERRAND_WORLD_PORT` (default 8765)
`+ 1000 + 8 * worker + slot`, where `worker` is n for xdist's `gwN` (0 in a serial run)
and `slot` is 0 to 7. The block starts 1000 above the world port, so it never lands on
the world, API or web port, and two workers never share a port.

## Hygiene

Enforced by the pre-commit hooks (`make setup` installs them) and again by
`make check` over every tracked file and every commit message:

- no em dashes in tracked text;
- no AI-tool attribution in commit messages;
- no credentials in tracked files, no tracked `.env`, and the local `.env` key value
  nowhere in the repo;
- the design note stays within its word budget;
- planner, model, context and reader code never import the executor, and the mock
  world never imports the agent (import-linter).

`backend/tests/hygiene/` proves these with real `git commit`s in a throwaway repo.

## Secrets

Copy `.env.example` to `.env` (gitignored) for the Claude API key. Offline tests and
`make evals` never read it.
