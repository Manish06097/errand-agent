# Spikes

Pre-design experiments against LangGraph, the Claude API and Langfuse. The design
(`docs/design/architecture.md`, the ADRs) cites them as "the spike". Every finding
below names the script that shows it, the raw file that records it and the number or
line to look for. These are throwaway scripts kept as evidence: they are not part of
the product, are not linted, and are left as they were written, with the one edit
listed under "Changes to the spike code".

## Provenance

| Spike | Run | Raw output |
|---|---|---|
| `claude/` | original run, 2026-11-06, against the paid API. Not re-run. | `claude/out/*.json`, written by the scripts themselves |
| `langgraph/` | re-run 2026-11-06 (13:02 UTC) with the original venv, via `langgraph/rerun.sh` | `langgraph/out/*.txt` (stdout and stderr), `*.ledger`, `s1_counter.json`; versions in `out/versions.txt` |
| `langfuse/` | re-run 2026-11-06 (13:02 UTC) with the original venv, via `langfuse/rerun.sh` | `langfuse/out/*.txt`; versions in `out/versions.txt` |

The original LangGraph run printed to stdout and kept only its ledgers and counter
file; those are in `langgraph/out/first-run/` and match the re-run. The original
Langfuse run saved nothing. Each spike's exact package set is in its
`requirements.txt` (LangGraph 1.2.20, langgraph-checkpoint-sqlite 3.1.3,
langchain-core 1.7.6, langchain-anthropic 1.7.10, aiosqlite 0.22.1, langfuse 5.2.0,
Python 3.11.11), the same pins as `backend/pyproject.toml`. Raw outputs contain the
absolute paths of the machine they ran on (in tracebacks and the version header).

No re-run result disagrees with what the design says.

## LangGraph (architecture 6.1, ADRs 0001, 0003, 0011, 0013)

| # | Finding | Script | Raw file and what to look for |
|---|---|---|---|
| L1 | The default durability, `"async"`, loses checkpoints on a crash: after `os._exit` inside `book`, `get_state` shows `next=()` for an unfinished run, and a restart re-runs `hold` and `book` | `scripts/s3_crash.py` (`run ... async` with `CRASH=1`, then `inspect`, `restart`) | `out/s3_crash.txt`, async block: `next=() values={'plan': 'P1@Tue09'} step=0`; `out/s3_async.ledger`: 2 `book` lines, 2 `hold` lines |
| L2 | With `durability="sync"` the checkpoint before `book` survives (`next=('book',)`, step 2) and only `book` re-runs. With `"exit"` nothing survives and the restart fails | `scripts/s3_crash.py` (`sync`, `exit`) | `out/s3_crash.txt`: `next=('book',) ... step=2`; `EmptyInputError` in the exit block; `out/s3_sync.ledger`: 2 `book` lines, 1 `hold` line |
| L3 | A resumed node re-runs from its first line | `scripts/s1_interrupt.py` (`start`, then `resume` in a second process) | `out/s1_interrupt.txt`: `approve_before_interrupt` goes from 1 to 2 after resume; `out/s1b_edge.txt`: `ask-enter` 3 times for a node with two interrupts |
| L4 | Two concurrent resumes on one thread both run the side effect; a per-thread lock plus a pending-interrupt check runs it once | `scripts/s4b_concurrency.py` | `out/s4b_concurrency.txt`: `book side effect ran 2 times`, then `book ran 1 times` with the lock |
| L5 | Plain input on an interrupted thread drops the pending interrupt and restarts from START | `scripts/s1b_edge.py` | `out/s1b_edge.txt`: `log: ['start', 'user said something else', 'start']`, `start` called twice |
| L6 | Sync `get_state` on `AsyncSqliteSaver` raises on the event-loop thread | `scripts/s4_stream.py` | `out/s4_stream.txt`: `InvalidStateError Synchronous calls to AsyncSqliteSaver are only allowed from a different thread` |
| L7 | `version="v2"` outputs are typed | `scripts/s1_interrupt.py` (`start_v2`), `scripts/s4_stream.py` | `out/s1_interrupt.txt`: `GraphOutput(value=..., interrupts=(...))`; `out/s4_stream.txt`: v2 parts with keys `['data', 'ns', 'type']` |
| L8 | Unregistered types in a checkpoint deserialize with a warning ("will be blocked in a future version"); an `allowed_msgpack_modules` allowlist silences it | `scripts/s6_serde.py` (`write`, `read`), `scripts/s6b_allow.py` | `out/s6_serde.txt`: `Deserializing unregistered type s6pkg.models.Phase`, then `with allowlist: Proposal` |
| L9 | LangGraph's checkpointer and our own write-ahead table can share one SQLite file, and the intent row lets a restart recover through a status check instead of booking twice | `scripts/s3b_writeahead.py` (`run` with `CRASH=1`, then `restart`) | `out/s3b_writeahead.txt`: tables `['booking_intents', 'checkpoints', 'writes']`; `provider bookings` has 1 row; `recovered: True` |

Added in the re-run, not in the original spike and not cited by the design: the same
`read` with `LANGGRAPH_STRICT_MSGPACK=true` succeeds without a warning
(`out/s6_serde.txt`, third block), because in strict mode `compile()` allowlists the
types reachable from the state schema. That is why architecture 6.1 requires every
state type to be reachable from `ThreadState`.

Supporting output nothing cites directly: `scripts/s3c_exc.py` (an exception in one
branch leaves `next=('b',)` and a resume re-runs only that branch; `RetryPolicy` made 3
attempts) and `scripts/s5_inspect.py` (`StateSnapshot` fields and history filters).

## Claude API (architecture 10, 13.1, 16.2; ADRs 0005, 0007, 0012)

| # | Finding | Script | Raw file and number |
|---|---|---|---|
| C1 | Thinking blocks are bound to the exact prefix. An append-only tool loop is accepted; editing an earlier user message or the system prompt and replaying the thinking is a 400, also without the beta header (enforced by default on this account); `drop_block` drops the blocks instead | `s3_thinking.py` | `out/s3_results.json`: `A_call1`..`A_call3` ok with `input_transformations: []`; `D_edit_earlier_user_error` and `E_edit_no_beta_default`: `400 ... The block is bound to a different conversation`; `G_changed_system_error`: `400 ... prompt differs from the one this block was created with`; `F_edit_drop_block`: 2 `thinking_dropped` transformations |
| C2 | Rebuilding a turn from our own state is always accepted: prior turn as plain text with a new system prompt, or prior tool calls kept with thinking stripped and the loop continued under `prefix_mismatch_behavior: "error"` | `s3_thinking.py`, `s3b_structured_history.py` | `out/s3_results.json`: `B_plaintext_rebuild_new_system` ok; `out/s3b_results.json`: 2 calls, `transforms: []` |
| C3 | A streamed, aggregated response (thinking with signature plus tool_use) round-trips under `"error"` across a 3-call loop | `s8_stream.py` | `out/s8_stream.json`: `thinking_sig_lens` 904 and 1016, 3 calls, no error |
| C4 | Characters per token, worst cases: 1.68 for Hindi-mixed text, 1.89 for compact JSON (same on Opus 5.5 and Haiku 5.5). Dividing UTF-8 bytes by 1.5 overcounts by at least 1.68 / 1.5 = 12% | `s5_tokens.py` | `out/s5_tokens.json`: `claude-opus-5-5:hindi_mixed` 396 chars / 236 tokens = 1.68; `claude-opus-5-5:json_compact` 1682 / 888 = 1.89; `one_tool_overhead` 386 |
| C5 | A Haiku 5.5 review-reader call costs about $0.0002 and flags both injected reviews with enum-only output | `s6_haiku_reader.py` | `out/s6_haiku.json`: `enum_only_low.cost_usd` 0.000192 (997 in, 185 out); `injection_suspected` true for `r2` and `r4` only |
| C6 | LangChain's `input_tokens` includes cache reads and writes, so they are subtracted before pricing. When the response carries the TTL split, LangChain reports cache writes only in `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` and sets the generic `cache_creation` to 0, so pricing must read the ephemeral fields (the generic field is a fallback only when both are 0). For Opus 5.5: `uncached = input_tokens - cache_read - write_5m - write_1h`, then `(uncached x 4 + read x 4 x 0.05 + write_5m x 4 x 1.25 + write_1h x 4 x 2 + output x 20) / 1e6` | `s4_usage_cache.py` (`cost_usd()`) | `out/s4_usage_cache.json`, call 1: `usage_metadata.input_tokens` 1854 = `raw_usage.input_tokens` 11 + `input_token_details.ephemeral_5m_input_tokens` 1843 (also `raw_usage.cache_creation_input_tokens` 1843), while `input_token_details.cache_creation` is 0; cost (11 x 4 + 1843 x 5 + 5 x 20) / 1e6 = 0.009359. Call 2: 1855 = raw 12 + `cache_read` 1843; cost (12 x 4 + 1843 x 0.2 + 5 x 20) / 1e6 = 0.000517 (0.0005166 rounded). No call wrote a 1 h entry (`ephemeral_1h_input_tokens` 0), so the 2x term is in the formula but not exercised |
| C7 | Forced `tool_choice` (`any` or a named tool) is rejected by Opus 5.5, and LangChain silently drops it when thinking is on, so steering cannot rely on it | `s2_tools.py`, `s5_tokens.py` | `out/s2_results.json`: `forced_any` and `forced_tool`: `400 ... tool_choice: type "tool" and "any" are not supported for this model`; `forced_with_thinking_warning`; `out/s5_tokens.json`: `count_tokens_forced` the same 400 |
| C8 | `with_structured_output(method="json_schema")` works with thinking on; `function_calling` warns | `s2_tools.py` | `out/s2_results.json`: `so_json_schema.parsed` set, `warnings: []`; `so_function_calling.warnings` |

`s1_basic.py` (payload shapes, `out/s1_basic.json`) is supporting only. `s7_fakes.py`
(which langchain-core fakes can drive a tool loop) printed to stdout and saved
nothing; it was not re-run, and no finding depends on it. The prices in
`s4_usage_cache.py` and `common.py` are inputs taken from the price list, not
measurements.

## Langfuse (architecture 16.4, ADR 0008)

No Langfuse server was running. The scripts point the SDK at a closed loopback port
or a blackhole address and capture spans with an in-memory exporter.

| # | Finding | Script | Raw file and what to look for |
|---|---|---|---|
| F1 | Unconfigured, `make_sink()` returns `NullSink` and `langfuse` is never imported | `scripts/t_sink.py` (with `scripts/langfuse_sink.py`) | `out/t_sink.txt`: `unconfigured -> NullSink \| langfuse imported: False` |
| F2 | With no keys, or with tracing disabled, the SDK makes no network call | `scripts/t_noop.py nokeys`, `disabled` | `out/t_noop.txt`: `NETWORK: []` in both blocks |
| F3 | Spans cannot be backfilled in v5: the end time can be set, the start time cannot, so spans are emitted live | `scripts/t_replay.py` | `out/t_replay.txt`: `start_offset_s=0.00 end_offset_s=-4.20` |
| F4 | Explicit `cost_details` reach the span | `scripts/t_sink.py` | `out/t_sink.txt`: `planner ... cost={"input": 0.0036, "output": 0.0024, "total": 0.006}` |
| F5 | `create_trace_id(seed=run_id)` gives a deterministic trace id that every span shares | `scripts/t_capture.py` | `out/t_capture.txt`: every span `trace_id=302aa610...`, equal to `trace_id_expected` |
| F6 | Session and trace name land on every span, children and events included, when each span start is wrapped in `propagate_attributes`; the kind mapping yields agent, generation, tool and event | `scripts/t_sink.py` | `out/t_sink.txt`: `session=conv-42 tname=errand_run` on all 4 spans; types `agent`, `generation`, `tool`, `event` |
| F7 | The LangChain `CallbackHandler` adds its own generation for each model call (a duplicate of ours) and chain spans whose input and output are the raw graph state | `scripts/t_capture.py` | `out/t_capture.txt`: span `GenericFakeChatModel` (generation) and spans `plan`, `errand_turn` with `input = {"q": ...}` |
| F8 | `flush()` blocks the caller for up to the client timeout when the server is unreachable; `shutdown()` afterwards returns at once | `scripts/t_blackhole.py` (`T=2`, `T=5`), `scripts/t_noop.py unreachable` | `out/t_blackhole.txt`: `flush blocked 2.0s (timeout=2)`, `flush blocked 5.0s (timeout=5)`, `shutdown 0.0s`; `out/t_noop.txt`: `flush=3.13s` against a closed port |

The private `TracerProvider` in 16.4 is a design choice, shown in
`scripts/langfuse_sink.py` (`Langfuse(tracer_provider=TracerProvider())`), not a
measurement.

### The downloaded compose file

`langfuse/downloaded/docker-compose.yml` is the upstream Langfuse self-hosting compose
file, committed unmodified because M15 builds on it.

- Source: `https://raw.githubusercontent.com/langfuse/langfuse/05ece6fd7b81c29e084b415f88fb230efcab62da/docker-compose.yml`
  (the last commit touching the file; downloaded from `main` on 2026-11-06 at 16:57 IST,
  and byte-identical to that commit when re-checked the same day).
- sha256: `cf67837af1f69e6a0d82125bd623e63401b1b8876bf467c8c22adb8c384c7ff2`
- It ships upstream placeholder credentials marked `# CHANGEME` (for example
  `postgres:postgres`, `SALT: mysalt` and an all-zero `ENCRYPTION_KEY`). They are
  local defaults, not secrets, and must be overridden before M15 runs it. The secret
  scan matches specific credential shapes, so they need no allowlist entry.
- Langfuse's model price table (`prices.json`, 274 KB) was downloaded alongside and is
  not committed: no finding cites it.

## Changes to the spike code

- `claude/common.py` line 5 read the API key from a hard-coded
  `/Users/recro/assignment/errand-agent/.env`. It now reads the path from
  `SPIKE_ENV_FILE`, defaulting to the repo-root `.env`. Nothing else changed.

Added for reproducibility, not part of the original spikes: `langgraph/rerun.sh`,
`langfuse/rerun.sh`, the three `requirements.txt` files and this README.

## What is not committed

Each spike's `.venv` (about 50 MB each), `__pycache__`, the 11 SQLite files the
LangGraph scripts wrote, and `prices.json`. The copy into this directory used an
explicit file list. Before committing, the copy was scanned with gitleaks 8.30.1
(`gitleaks dir spikes`: no leaks found) as well as our own `scripts/secret_scan.py`.

## Reproducing

```sh
cd spikes/langgraph            # or spikes/langfuse
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./rerun.sh                     # rewrites out/*.txt
```

The LangGraph and Langfuse scripts need no network and no keys. The Claude scripts
call the paid API with the key from `SPIKE_ENV_FILE` (default: the repo-root `.env`)
and overwrite `claude/out/`; they are kept for inspection, not for routine re-runs.
