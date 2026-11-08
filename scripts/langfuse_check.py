"""Live check (A5): Langfuse shows what the JSONL says, for every run of one thread.

    uv run --project backend python scripts/langfuse_check.py --thread c_0001

Needs a running Langfuse (`make langfuse-up`) that the API exported to; it is never part
of the offline gate. For each run in data/traces/<thread>/*.jsonl it polls Langfuse's
public API until a deadline (ingestion lags 15 to 30 s) and checks:
- observations exist under the run's own trace id, with one `agent` root;
- every observation's session is the thread;
- the generations' total cost equals `run.end.totals.cost_usd` (ours, not Langfuse's
  price table, which knows these models at different prices);
- one generation per model call, and one observation per JSONL span id.
Exits 1 on any mismatch, 2 when it cannot run at all. Keys and host come from LANGFUSE_*
or else from infra/langfuse/.env; neither key is ever printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
INFRA_ENV = REPO / "infra" / "langfuse" / ".env"
COST_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Run:
    run_id: str
    trace_id: str
    thread_id: str
    cost_usd: float
    model_calls: int
    outcome: str
    spans: int  # distinct span ids in the JSONL: one observation each


@dataclass
class Result:
    run: Run
    problems: list[str]
    seen: dict[str, Any]


def credentials() -> tuple[str, str, str]:
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    if (not public or not secret) and INFRA_ENV.is_file():
        values: dict[str, str] = {}
        for raw in INFRA_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip("'\"")
        public = public or values.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY")
        secret = secret or values.get("LANGFUSE_INIT_PROJECT_SECRET_KEY")
        host = host or f"http://localhost:{values.get('ERRAND_LANGFUSE_PORT', '3100')}"
    if not public or not secret:
        sys.exit("[langfuse-check] no Langfuse keys: set LANGFUSE_* or run make langfuse-up")
    return public, secret, (host or "http://localhost:3100").rstrip("/")


def runs_of(traces_dir: Path, thread: str) -> list[Run]:
    folder = traces_dir / thread
    files = sorted(folder.glob("*.jsonl"))
    if not files:
        sys.exit(f"[langfuse-check] no trace files under {folder}")
    runs = []
    for path in files:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        start = next(r for r in records if r["type"] == "run.start")
        end = next((r for r in records if r["type"] == "run.end"), None)
        if end is None:
            sys.exit(f"[langfuse-check] {path.name} has no run.end; is the run still going?")
        runs.append(
            Run(
                run_id=start["run_id"],
                trace_id=start["trace_id"],
                thread_id=start["thread_id"],
                cost_usd=float(end["totals"]["cost_usd"]),
                model_calls=int(end["totals"]["model_calls"]),
                outcome=str(end["outcome"]),
                spans=len({r["span_id"] for r in records}),
            )
        )
    return sorted(runs, key=lambda r: r.run_id)


class Api:
    def __init__(self, host: str, public: str, secret: str) -> None:
        self.host = host
        token = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
        self._auth = f"Basic {token}"

    def get(self, path: str, **query: Any) -> Any:
        url = f"{self.host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise


def observations(api: Api, trace_id: str) -> list[dict[str, Any]]:
    """Every observation of one trace. Langfuse v4 is observations-first: the v1 traces
    endpoints answer 404 in its events_only mode, and the session, name and costs live
    on the observations themselves."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query: dict[str, Any] = {"traceId": trace_id, "limit": 100, "fields": "core,basic,usage"}
        if cursor:
            query["cursor"] = cursor
        page = api.get("/api/public/v2/observations", **query) or {}
        found.extend(page.get("data") or [])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            return found


def check(api: Api, run: Run) -> Result:
    seen_obs = observations(api, run.trace_id)
    if not seen_obs:
        return Result(run, [f"no observations under trace {run.trace_id}"], {})
    roots = [o for o in seen_obs if o.get("isRootObservation")]
    generations = [o for o in seen_obs if str(o.get("type", "")).upper() == "GENERATION"]
    sessions = sorted({str(o.get("sessionId")) for o in seen_obs})
    cost = sum(float(o.get("totalCost") or 0.0) for o in generations)
    seen = {
        "session": ",".join(sessions),
        "cost_usd": round(cost, 10),
        "generations": len(generations),
        "observations": len(seen_obs),
    }
    problems = []
    if len(roots) != 1 or str(roots[0].get("type", "")).upper() != "AGENT":
        problems.append(f"expected one AGENT root, got {[o.get('type') for o in roots]}")
    if sessions != [run.thread_id]:
        problems.append(f"sessions {sessions} != thread {run.thread_id!r}")
    if abs(cost - run.cost_usd) > COST_TOLERANCE:
        problems.append(f"cost {cost} != run.end {run.cost_usd}")
    if len(generations) != run.model_calls:
        problems.append(f"{len(generations)} generations != {run.model_calls} model calls")
    if len(seen_obs) != run.spans:
        problems.append(f"{len(seen_obs)} observations != {run.spans} JSONL spans")
    return Result(run, problems, seen)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--thread", required=True)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("ERRAND_DATA_DIR", "data"))
    )
    parser.add_argument("--deadline", type=float, default=120.0, help="seconds to keep polling")
    args = parser.parse_args(argv)

    runs = runs_of(args.data_dir / "traces", args.thread)
    public, secret, host = credentials()
    api = Api(host, public, secret)
    print(f"[langfuse-check] {len(runs)} runs of {args.thread}, Langfuse at {host}")

    started = time.monotonic()
    results: dict[str, Result] = {}
    unreachable: str | None = None
    while True:
        try:
            results = {run.run_id: check(api, run) for run in runs}
            unreachable = None
        except (urllib.error.URLError, OSError) as exc:
            # A restarting server or a slow query is not a mismatch; keep polling.
            unreachable = str(exc)
        pending = unreachable is not None or any(r.problems for r in results.values())
        if not pending or time.monotonic() - started >= args.deadline:
            break
        time.sleep(5)
    if unreachable is not None and not results:
        print(f"[langfuse-check] cannot reach {host}: {unreachable}", file=sys.stderr)
        return 2
    waited = time.monotonic() - started

    failed = 0
    for result in results.values():
        run = result.run
        status = "ok  " if not result.problems else "FAIL"
        failed += bool(result.problems)
        print(
            f"{status} {run.run_id} {run.outcome:<18} trace={run.trace_id} "
            f"session={result.seen.get('session')} cost={result.seen.get('cost_usd')} "
            f"(jsonl {run.cost_usd}) generations={result.seen.get('generations')} "
            f"(model calls {run.model_calls}) observations={result.seen.get('observations')} "
            f"(spans {run.spans})"
        )
        for problem in result.problems:
            print(f"       {problem}")
    print(
        f"[langfuse-check] {len(runs) - failed}/{len(runs)} runs match "
        f"after {waited:.0f}s of polling"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
