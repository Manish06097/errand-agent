# Errand agent

An agent that arranges home-service errands for one user in a mock Bengaluru: find a plumber, electrician or AC servicer, compare them, book, cancel or reschedule, and notice on its own when a provider cancels. It asks before it acts. A Claude model (Opus 5.5 as the planner, Haiku 5.5 as a sandboxed reader of provider reviews) understands the request and proposes; code holds the slot, checks the approval, makes the booking call and decides whether it worked.

The brief's three example requests are covered: book a plumber in Indiranagar this week under a budget, move an AC servicing to Saturday, and "same electrician as last time, not before 10". Cancel is built too. The system never says "booked" until the provider's own record shows the booking, and it survives a `book` call that times out after succeeding, a restart in the middle of a booking, and a provider cancelling a booking later.

## Quick start

Requirements: uv (it fetches Python 3.11), Node 20.9 or newer, pnpm 10.24 or newer, git and make. macOS or Linux.

```sh
make setup                       # backend and web dependencies from the lockfiles, git hooks
cp .env.example .env             # then put your Claude API key in .env (gitignored)
make dev                         # world 8765, API 8000, web 3200
```

Open http://localhost:3200. `make dev` plans with a scripted planner by default, which needs no key. To talk to the real models, run `ERRAND_MODEL_MODE=live make dev`. Ports come from `ERRAND_WORLD_PORT`, `ERRAND_API_PORT` and `ERRAND_WEB_PORT`; if one is taken `make dev` stops before starting anything and names the variable. A terminal client over the same API: `uv run --project backend errand chat --api http://127.0.0.1:8000`.

The dev drawer (the web app, with `ERRAND_DEV=1`, which `make dev` sets) has a scenario picker, a clock control, "Next book times out after success" and "Provider cancels booking". The demo path is in [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

## How it works

A short tour. The long form is [docs/design/architecture.md](docs/design/architecture.md), the one-page summary is [docs/DESIGN_NOTE.md](docs/DESIGN_NOTE.md), and the load-bearing decisions are in [docs/adr/](docs/adr/).

- **The model proposes, code disposes.** A LangGraph graph with one interrupt (the approval) and the planner's tool loop inside one node ([section 6](docs/design/architecture.md#6-orchestration-with-langgraph), ADR 0001 and 0012). The model reads, asks, and proposes. Only code holds a slot, grants an approval, calls `book`, `cancel` or `reschedule`, and writes the outcome.
- **The approval fingerprint.** A card carries a hash of the material facts (user, provider, service, slot, held quote). A grant is a button press with that fingerprint, or a typed "book it" bound to the latest rendered card. Stale, repeated or ambiguous input never grants ([section 8](docs/design/architecture.md#8-approval-enforcement), ADR 0002).
- **Write-ahead intents and verify.** Consuming an approval writes an intent with a derived idempotency key before `book` is sent. After any answer, including a timeout, `verify` asks `get_booking_status` by that key and re-reads the bookings. A booking that differs from the approval is never called booked ([section 9](docs/design/architecture.md#9-idempotency-and-recovery), ADR 0003).
- **The reconciler.** Before the API serves and every 30 s it reads our own tables, verifies each unsettled attempt by its key, ends runs a dead process left behind, and expires a grant nobody acted on.
- **Untrusted text.** Provider reviews are read only by a quarantined Haiku call that returns labels from a closed set. The planner never sees review text ([section 11](docs/design/architecture.md#11-untrusted-output-and-the-quarantined-reader), ADR 0005).
- **Memory and the context budget.** Typed preference rows with provenance, explicit versus inferred, inspect, correct and forget (final in `agent.sqlite`, masked on read in traces), and a 12,000-token request cap built by code ([sections 12 and 13](docs/design/architecture.md#12-memory-model), ADR 0006 and 0007).
- **The follow-up watcher.** It detects a provider cancel with no user prompt, decides whether to notify, searches a replacement within current preferences and offers it on a new card that needs a fresh approval ([section 14](docs/design/architecture.md#14-proactive-follow-up), ADR 0014).
- **The trace of record.** Every run writes a JSONL trace that is the source for the trace panel, the evals and the exported conversation in [docs/traces/](docs/traces/). Langfuse is optional and off by default ([section 16](docs/design/architecture.md#16-observability), ADR 0008).

## Evals

```sh
make evals      # offline, no key, no network; regenerates docs/EVAL_TABLE.md
```

[docs/EVAL_TABLE.md](docs/EVAL_TABLE.md) is generated from `backend/evals/scenarios/*.yaml`, one row per scenario and mode, with the invariants each row checked (INV-01 to INV-20: no booking without a matching approval, no duplicate side effect, no success claim without a record, no review text in the planner's context, and so on).

What the `model_mode` column means:

- **scripted**: a reference planner I wrote, plus a keyword reader. Most rows are this. They prove the code around the model: the guards, the approval, recovery, the invariants. They do not prove the model behaves well.
- **hostile**: a planner built to misbehave (re-ask a question, claim a booking that does not exist, propose a changed price). The invariants must hold anyway, which shows the enforcement lives in code.
- **replay**: the real Opus and Haiku responses recorded once with `make record SCENARIO=S01` and replayed offline, byte-checked against the request. Today two scenarios have a cassette: S01 (the happy path) and S22 (the timeout after success). A replay row keeps the scripted row's steps and checks, so the real model may take a different valid path. A recorded request that no longer matches fails loudly instead of being served.
- **live**: the real API, not part of `make evals`. `ERRAND_MODEL_MODE=live make dev` is how I ran the conversation in [docs/traces/timeout_after_success.md](docs/traces/timeout_after_success.md).

Say it plainly: the real-model evidence is two cassettes and a handful of live conversations, not a statistical result.

## Limits

Read from `backend/src/errand/config.py` (`LimitSettings`, `ERRAND_LIMIT_*`). Every run ends in an outcome from a closed set, whatever cap it hits.

| Limit | Value |
|---|---|
| Model turns per run | 8 (loop iterations 6) |
| Tool calls per run | 16 |
| Wall clock per run | 60 s offline, 43 s with the real model, 90 s outer backstop |
| Extra time an open booking intent may take to finish verifying | 20 s |
| Context cap per planner request | 12,000 estimated tokens |
| Reader cost cap per run | $0.40 |
| Reprompts, replans, status checks per run | 1, 1, 4 |

## Cost and latency

Measured with `errand-trace stats --data-dir data` over three real planning turns (one run per user message, Opus 5.5 at medium effort plus the Haiku reader) from the live dev conversations on 2026-11-09, running the product code at `30fa803` (no product code changed since). A sample of three is small; read it as an order of magnitude.

| | p50 | p95 |
|---|---|---|
| Latency per planning turn | 10.1 s | 11.1 s |
| Cost per planning turn | $0.021 | $0.024 |

The whole timeout-after-success conversation (three runs, four planner calls, one reader call that missed the cache) cost $0.033; see `conversation.totals` in [docs/traces/timeout_after_success.jsonl](docs/traces/timeout_after_success.jsonl). Recording the S22 cassette cost $0.025.

## Known limitations

- Most of the eval table runs on a planner I wrote. Real-model coverage is the S01 and S22 cassettes and manual live runs. There is no live-smoke suite and no cassette for the reschedule, injected-review or provider-cancel scenarios (S07, S14, S39) yet.
- The scope trim cut, on purpose: fault points beyond the three restarts need, lease-epoch fencing, a periodic scan of expired-lease runs, resuming a granted approval, a choice card for an ambiguous "last electrician" (a plain question lists the matches), a second-model fallback on refusal, a per-run cost cap on the planner (it applies to the reader only), the pending-cancel-card rule, the race between a follow-up and a same-chat replacement, a crash in the middle of a follow-up, more Langfuse work, a cassette per brief category, a prompt-tuning pass, and 19 planned scenarios (the catalogue has 83 files against about 90 planned).
- Open follow-ups from the proactive-follow-up work: the follow-up does not yet apply memory to the replacement search or use a budget ceiling from memory; there is no `deferred` follow-up state, so `contact_window` does not delay a notification; S39b and S39c and the matching INV-17 clause are not written; a recovered notification after a restart is missing.
- Memory: the API and the forget path exist (`/api/memory`) and the planner pre-fills from memory, but the `/memory` page, the planner's memory tools and the memory variant of example 3 are not in this build; they are the next piece of work.
- Holds are placed when a card is proposed, before the click. They are free, expire in ten minutes and are not treated as consequential. There is no setting to ask first.
- A re-posted approval for an already-consumed card answers 200 `already_approved`, not 409; nothing is booked twice.
- The user event stream polls the database every 250 ms. A follow-up skips the review reader, so its ranking has no review signal.
- The mock world's timeouts, 503s and slot conflicts are drawn from a seeded generator with named faults; the real providers of the brief are not available, so "exactly as the brief's tools behave" is as far as this goes. Argument validation and the test-only faults are listed in architecture section 0.
- One user (`u_demo`), SQLite, no authentication. This is a demo, not a deployment.

## Memory and retention

The agent remembers stable preferences you state ("I live in Indiranagar", "never before 10"), keeps the ones it guessed apart until you click Keep, and lets you see, correct and forget each one. Forgetting is final in `agent.sqlite`: the value is overwritten in every message you or the assistant wrote that held it and then removed from its row. Notices are redacted too, since the step-limit notice restates the brief. Other text the program renders itself, such as a booking line or a status line, records what happened and is left as it is. Three things outlive it on purpose. The JSONL traces under `data/`, older checkpoints of a conversation and Langfuse (when it is on) keep a forgotten value until you delete them; the app masks it on every read it serves but does not rewrite those files. The HMAC hashes that drive the masking stop the value being shown again, not someone who holds both `data/memory_hmac.key` and the database. And bookings stay: forgetting a preferred provider does not erase the booking history.

Data retention: everything the app writes goes under `data/` (gitignored): `agent.sqlite`, the checkpoints, `world.sqlite` (the mock world), `memory_hmac.key` and the traces. Nothing expires on its own and nothing leaves the machine except the requests to the Claude API (and to Langfuse, if you turn it on). Deleting `data/` deletes it all. Traces hold sanitized records: review text is hashed, and phone numbers, addresses and keys are masked, once, when each record is written. The exported trace in `docs/traces/` went through that and through the masked reader.

## Commands

| Command | What it does |
|---|---|
| `make setup` | Installs backend and web dependencies from the lockfiles and the git hooks. |
| `make dev` | Starts the world (8765), the API (8000) and the web app (3200) with prefixed logs. Stops all three if one exits or on Ctrl-C. |
| `make check` | `uv sync --locked`, ruff, ruff format, mypy, import-linter, the em-dash, attribution and secret guards, the design-note budget, `pytest backend/tests` in two steps (fast tests under xdist, then the `slow` ones that start subprocesses or servers), web typecheck and lint, and the generated-types check. |
| `make evals` | The offline eval suite, no key. Regenerates `docs/EVAL_TABLE.md` and `docs/eval_results.json`. It fails if no scenario is collected. |
| `make e2e` | Playwright (`web/e2e`): starts its own stack (scripted planner, fresh data dir, `ERRAND_DEV=1`, your ports), runs the browser tests, stops it. Installs chromium first. |
| `make record SCENARIO=S01` | Records the scenario's cassette (`backend/evals/cassettes`) with the real Opus and Haiku and the key in `.env`, and prints the cost. |
| `errand-trace show <run>`, `export --thread <id>`, `excerpt <file> --kind timeout`, `stats` | The trace tooling: a run's timeline, a conversation-level export with a header and totals record, the design note's excerpt, and latency and cost of live planning turns. Run as `uv run --frozen --project backend errand-trace ...`. |
| `make langfuse-up`, `make langfuse-down` | Optional self-hosted Langfuse on 127.0.0.1:3100 (about 4 GiB of Docker memory). Nothing else needs it. |

The tests bind port 0 wherever they can. A test that needs a fixed port takes it from `worker_port(slot)` in `backend/tests/conftest.py`: the world port `+ 1000 + 8 * worker + slot`.

## Hygiene and secrets

Enforced by the pre-commit hooks and again by `make check` over every tracked file and every commit message:

- no em dashes in tracked text;
- no AI-tool attribution in commit messages;
- no credentials in tracked files, no tracked `.env`, and the local `.env` key value nowhere in the repo;
- the design note stays within its word budget (550 words, a 12-line excerpt);
- planner, model, context and reader code never import the executor, and the mock world never imports the agent (import-linter).

`backend/tests/hygiene/` proves these with real `git commit`s in a throwaway repo. Copy `.env.example` to `.env` for the Claude API key; offline tests and `make evals` never read it.

## Deliverables

| | Where |
|---|---|
| Design note (one page) | [docs/DESIGN_NOTE.md](docs/DESIGN_NOTE.md) |
| Eval table (generated) | [docs/EVAL_TABLE.md](docs/EVAL_TABLE.md), [docs/eval_results.json](docs/eval_results.json) |
| Three bugs written up, and the full log | [docs/BUGS_WRITEUP.md](docs/BUGS_WRITEUP.md), [docs/BUGS.md](docs/BUGS.md) |
| Timeout-after-success trace and its annotated excerpt | [docs/traces/timeout_after_success.jsonl](docs/traces/timeout_after_success.jsonl), [docs/traces/timeout_after_success.md](docs/traces/timeout_after_success.md) |
| Demo script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |
| Architecture, ADRs, build journal | [docs/design/](docs/design/), [docs/adr/](docs/adr/), [docs/engineering/journal.md](docs/engineering/journal.md) |
| AI-assistant session transcripts | `docs/ai-transcripts/` - placeholder: the author adds them before submission; the folder does not exist yet |
