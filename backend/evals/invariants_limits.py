"""INV-09: counters never exceed the limits the run recorded (architecture 15.1, 18.5).

It reads the trace and nothing else, and takes every limit from the run's own
`run.start` record (`limits`), so it never imports config or product code: a cap that is
loosened in code shows up as a changed record, and a cap that stops binding shows up as a
violation. Clauses, per run:

- model calls (every `model.call`, failed ones included) <= model_calls;
- model calls inside one `plan` node <= iterations;
- tool calls outside the critical section (execute and verify) <= tool_calls;
- reprompts (`guard.reprompt`) <= reprompts; plan invocations after the first <= replans;
- status checks (`get_booking_status` calls) <= status_checks;
- simulated elapsed (`run.end` minus `run.start`) <= deadline_s with no intent open, and
  <= deadline_s + critical_grace_s once an intent was written.

Each clause has a negative test on doctored records (tests/evals/test_invariants_limits.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evals.invariants import Records, Violations

CRITICAL_NODES = frozenset({"execute", "verify"})


def inv09_counters_within_limits(rec: Records) -> Violations:
    out: Violations = []
    for run, records in rec.run_records():
        out += check_run(run["run_id"], records)
    return out


def check_run(run_id: str, records: list[dict[str, Any]]) -> list[str]:
    start = next((r for r in records if r["type"] == "run.start"), None)
    end = next((r for r in records if r["type"] == "run.end"), None)
    if start is None:
        return [f"{run_id}: no run.start"]
    limits = start.get("limits")
    if not limits:
        return [f"{run_id}: run.start records no limits, so INV-09 cannot be checked"]
    out: list[str] = []
    model_calls = tool_calls = reprompts = status_checks = 0
    plan_starts = in_plan_calls = 0
    in_plan = False
    critical = 0
    intent = False
    for r in records:
        kind = r["type"]
        if kind == "node.start":
            if r["node"] == "plan":
                plan_starts += 1
                in_plan, in_plan_calls = True, 0
            if r["node"] in CRITICAL_NODES:
                critical += 1
        elif kind == "node.end":
            if r["node"] == "plan":
                in_plan = False
            if r["node"] in CRITICAL_NODES:
                critical = max(0, critical - 1)
        elif kind == "model.call":
            model_calls += 1
            if in_plan:
                in_plan_calls += 1
                if in_plan_calls > limits["iterations"]:
                    out.append(
                        f"{run_id} seq {r['seq']}: {in_plan_calls} model calls in one plan, "
                        f"over {limits['iterations']} iterations"
                    )
        elif kind == "tool.call":
            if r["tool"] == "get_booking_status":
                status_checks += 1
            if not critical:
                tool_calls += 1
        elif kind == "guard.reprompt":
            reprompts += 1
        elif kind == "intent.written":
            intent = True
    if model_calls > limits["model_calls"]:
        out.append(f"{run_id}: {model_calls} model calls, over {limits['model_calls']}")
    if tool_calls > limits["tool_calls"]:
        out.append(
            f"{run_id}: {tool_calls} tool calls outside the critical section, "
            f"over {limits['tool_calls']}"
        )
    if reprompts > limits["reprompts"]:
        out.append(f"{run_id}: {reprompts} reprompts, over {limits['reprompts']}")
    if max(0, plan_starts - 1) > limits["replans"]:
        out.append(f"{run_id}: {plan_starts - 1} replans, over {limits['replans']}")
    if status_checks > limits["status_checks"]:
        out.append(f"{run_id}: {status_checks} status checks, over {limits['status_checks']}")
    if end is not None:
        elapsed = (_ts(end) - _ts(start)).total_seconds()
        allowed = limits["deadline_s"] + (limits["critical_grace_s"] if intent else 0)
        if elapsed > allowed + 1e-6:
            what = "with an intent open" if intent else "with no intent open"
            out.append(f"{run_id}: ran {elapsed:.1f} s {what}, over {allowed:g} s")
    return out


def _ts(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(record["ts"]))
