"""errand-trace: inspect JSONL traces.

    errand-trace show <run_id | path.jsonl> [--data-dir DIR]

`show` reads through the masked reader, so a value the user has forgotten shows as
"[removed]" even if the raw file still holds it. It prints a timeline: seq, milliseconds
since the first record, the record type indented by span depth, and a one-line summary.
A truncated last line is reported on stderr and skipped.
`export`, `excerpt` and `stats` arrive in later milestones.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.config import PathSettings
from errand.trace.masked import MaskUnavailable, masker_for
from errand.trace.reader import TraceFormatError, read_trace_masked


def resolve(target: str, traces_dir: Path) -> Path | None:
    path = Path(target)
    if path.suffix == ".jsonl" or path.exists():
        return path if path.is_file() else None
    matches = sorted(traces_dir.glob(f"*/{target}.jsonl"))
    return matches[0] if len(matches) == 1 else None


def _fmt_cost(value: Any) -> str:
    return f"${value:.7f}".rstrip("0").rstrip(".") if isinstance(value, int | float) else "$?"


def _model_call(r: Mapping[str, Any]) -> str:
    usage = r.get("usage") or {}
    parts = [
        str(r.get("model")),
        f"in={usage.get('input_uncached', 0)}+r{usage.get('cache_read', 0)}"
        f"+w{usage.get('cache_write_5m', 0) + usage.get('cache_write_1h', 0)}",
        f"out={usage.get('output', 0)}",
        _fmt_cost(r.get("cost_usd")),
        f"{r.get('latency_ms', 0):.0f}ms",
        f"stop={r.get('stop_reason')}",
    ]
    calls = [c.get("name") for c in (r.get("response") or {}).get("tool_calls") or []]
    if calls:
        parts.append("calls=" + ",".join(str(c) for c in calls))
    return " ".join(parts)


def _run_end(r: Mapping[str, Any]) -> str:
    t = r.get("totals") or {}
    return (
        f"{r.get('outcome')} model_calls={t.get('model_calls')} tool_calls={t.get('tool_calls')} "
        f"tokens={t.get('tokens_in')}/{t.get('tokens_out')} {_fmt_cost(t.get('cost_usd'))}"
    )


def _tool_call(r: Mapping[str, Any]) -> str:
    outcome = f"error={r['error']}" if r.get("error") else "ok"
    return f"{r.get('tool')} by {r.get('caller')} attempt={r.get('attempt')} {outcome}"


_SUMMARIES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "model.call": _model_call,
    "run.end": _run_end,
    "tool.call": _tool_call,
    "run.start": lambda r: f"trigger={r.get('trigger')}",
    "node.start": lambda r: f"{r.get('node')} (attempt {r.get('node_attempt')})",
    "node.end": lambda r: f"{r.get('node')}" + (f" error={r['error']}" if r.get("error") else ""),
    "state.transition": lambda r: f"{r.get('from')} -> {r.get('to')} ({r.get('reason')})",
    "context.built": lambda r: " ".join(
        f"{k}={v}" for k, v in (r.get("manifest") or {}).items() if not isinstance(v, dict | list)
    ),
    "error": lambda r: f"{r.get('where')}: {r.get('exception_type')}: {r.get('message')}",
}
_ENVELOPE = {
    "v",
    "seq",
    "ts",
    "wall_ts",
    "run_id",
    "thread_id",
    "errand_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "node_attempt",
    "type",
}


def summarize(record: Mapping[str, Any]) -> str:
    fn = _SUMMARIES.get(str(record.get("type")))
    if fn is not None:
        return fn(record)
    fields = [f"{k}={v}" for k, v in record.items() if k not in _ENVELOPE and v is not None]
    return " ".join(fields)[:160]


def _depths(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    parents: dict[str, str | None] = {}
    for r in records:
        parents.setdefault(str(r.get("span_id")), r.get("parent_span_id"))
    depths: dict[str, int] = {}
    for span in parents:
        depth, seen, cur = 0, set(), parents.get(span)
        while cur is not None and cur not in seen and depth < 64:
            seen.add(cur)
            depth += 1
            cur = parents.get(cur)
        depths[span] = depth
    return depths


def _offset_ms(first: str | None, ts: str | None) -> str:
    if first is None or ts is None:
        return "?"
    try:
        delta = datetime.fromisoformat(ts) - datetime.fromisoformat(first)
    except ValueError:
        return "?"
    return f"{delta.total_seconds() * 1000:.0f}"


def render(records: Sequence[Mapping[str, Any]]) -> list[str]:
    if not records:
        return ["(empty trace)"]
    head = records[0]
    lines = [
        f"run {head.get('run_id')}  thread {head.get('thread_id')}  "
        f"trace {head.get('trace_id')}  ({len(records)} records)",
        f"{'seq':>5} {'+ms':>7}  type / detail",
    ]
    depths = _depths(records)
    first_ts = head.get("ts")
    for r in records:
        indent = "  " * depths.get(str(r.get("span_id")), 0)
        lines.append(
            f"{r.get('seq', '?'):>5} {_offset_ms(first_ts, r.get('ts')):>7}  "
            f"{indent}{r.get('type')}  {summarize(r)}".rstrip()
        )
    return lines


def show(target: str, data_dir: Path) -> int:
    path = resolve(target, data_dir / "traces")
    if path is None:
        print(
            f"errand-trace: no single trace found for {target!r} under {data_dir}", file=sys.stderr
        )
        return 2
    try:
        # A forgotten value never leaves the process unmasked (gate 2 decision 14).
        trace = read_trace_masked(path, masker_for(data_dir))
    except TraceFormatError as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 1
    except MaskUnavailable as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 3
    print(f"{path}")
    for line in render(trace.records):
        print(line)
    if trace.truncated_tail is not None:
        print(
            f"errand-trace: warning: last line of {path} is truncated "
            f"({len(trace.truncated_tail)} bytes ignored)",
            file=sys.stderr,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="errand-trace", description="Inspect errand traces.")
    sub = parser.add_subparsers(dest="command", required=True)
    show_p = sub.add_parser("show", help="print a run's timeline")
    show_p.add_argument("target", help="a run id, or the path of a .jsonl trace")
    show_p.add_argument(
        "--data-dir", type=Path, default=None, help="default: ERRAND_DATA_DIR or ./data"
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir or PathSettings().data_dir
    if args.command == "show":
        return show(args.target, data_dir)
    parser.error(f"unknown command {args.command}")  # pragma: no cover - argparse exits first
    return 2


if __name__ == "__main__":
    sys.exit(main())
