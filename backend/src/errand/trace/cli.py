"""errand-trace: inspect JSONL traces.

    errand-trace show <run_id | path.jsonl> [--data-dir DIR]
    errand-trace export --thread <conversation_id> [--out PATH] [--data-dir DIR]
    errand-trace excerpt <export.jsonl> --kind timeout

`show` reads through the masked reader, so a value the user has forgotten shows as
"[removed]" even if the raw file still holds it. It prints a timeline: seq, milliseconds
since the first record, the record type indented by span depth, and a one-line summary.
A truncated last line is reported on stderr and skipped.

`export` writes every run of one conversation, in the order the runs started, as one
JSONL (each record as the run wrote it, one per line), to `--out` or stdout. It reads each
run through the masked reader, never the raw file, so a forgotten value is masked in the
export too. Non-ASCII is escaped, so the file reads the same everywhere and a tracked copy
cannot carry an em dash. The file opens with one `export.header` record (run ids, git SHA,
model and clock mode) and closes with one `conversation.totals` record summed from the
records in between. `excerpt --kind timeout` prints at most 12 lines of it for the design
note.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from errand.clock import wall_now
from errand.config import PathSettings
from errand.trace.masked import MaskUnavailable, masker_for
from errand.trace.reader import TraceFormatError, read_trace, read_trace_masked
from errand.trace.schema import ConversationTotals, ExportHeader


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


def thread_runs(thread: str, traces_dir: Path) -> list[Path]:
    """The run traces of one conversation, in the order the runs started."""
    folder = traces_dir / thread
    if not folder.is_dir():
        return []

    def started(path: Path) -> tuple[str, str]:
        records = read_trace(path).records
        first = records[0] if records else {}
        return (str(first.get("wall_ts") or first.get("ts") or ""), path.name)

    return sorted(folder.glob("*.jsonl"), key=started)


def _checkout_sha() -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _clock_mode(records: Sequence[Mapping[str, Any]]) -> str:
    """`system_offset` when `ts` runs at wall speed behind a constant offset, `sim` when
    the simulated clock moved on its own (a skew of two seconds or more across the file)."""
    skews: list[float] = []
    for r in records:
        try:
            ts = datetime.fromisoformat(str(r["ts"]))
            wall = datetime.fromisoformat(str(r["wall_ts"]))
        except (KeyError, ValueError):
            continue
        skews.append((ts - wall).total_seconds())
    return "system_offset" if skews and max(skews) - min(skews) < 2.0 else "sim"


def header_record(thread: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    run_ids: list[str] = []
    for r in records:
        if r.get("run_id") not in run_ids:
            run_ids.append(str(r["run_id"]))
    recorded = sorted({str(r["git_sha"]) for r in records if r.get("git_sha")})
    modes = sorted({str(r["mode"]) for r in records if r.get("type") == "model.call"})
    return {
        "v": 1,
        "type": "export.header",
        "thread_id": thread,
        "run_ids": run_ids,
        "git_sha": ",".join(recorded) if recorded else _checkout_sha(),
        "git_sha_source": "run_records" if recorded else "exporting_checkout",
        "model_mode": "+".join(modes) if modes else "none",
        "clock_mode": _clock_mode(records),
        "exported_at": wall_now().isoformat(timespec="seconds"),
    }


def totals_record(thread: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summed from the exported records, as the tracer sums run.end: reader calls that
    reached the model count as reader calls, never as model calls."""
    cost = Decimal(0)
    tokens_in = tokens_out = model_calls = reader_calls = tool_calls = 0
    for r in records:
        kind = r.get("type")
        if kind == "tool.call":
            tool_calls += 1
        elif kind in ("model.call", "reader.call"):
            if kind == "model.call":
                model_calls += 1
            elif not r.get("cache_hit") and r.get("usage") is not None:
                reader_calls += 1
            usage = r.get("usage")
            if usage is not None:
                tokens_in += sum(
                    int(usage.get(k, 0))
                    for k in ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h")
                )
                tokens_out += int(usage.get("output", 0))
            if r.get("cost_usd") is not None:
                cost += Decimal(str(r["cost_usd"]))
    return {
        "v": 1,
        "type": "conversation.totals",
        "thread_id": thread,
        "runs": len({r.get("run_id") for r in records}),
        "model_calls": model_calls,
        "reader_calls": reader_calls,
        "tool_calls": tool_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": float(cost),
    }


def excerpt_file(path: Path, kind: str) -> int:
    from errand.trace.excerpt import ExcerptError, excerpt

    try:
        records = read_trace(path).records
        lines = excerpt(records, kind)
    except (OSError, TraceFormatError) as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 1
    except ExcerptError as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def export_thread(thread: str, data_dir: Path, out: Path | None) -> int:
    try:
        paths = thread_runs(thread, data_dir / "traces")
    except TraceFormatError as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 1
    if not paths:
        print(
            f"errand-trace: no runs found for thread {thread!r} under {data_dir}", file=sys.stderr
        )
        return 2
    lines: list[str] = []
    run_records: list[dict[str, Any]] = []
    try:
        masker = masker_for(data_dir)
        for path in paths:
            trace = read_trace_masked(path, masker)
            if trace.truncated_tail is not None:
                print(
                    f"errand-trace: warning: last line of {path} is truncated "
                    f"({len(trace.truncated_tail)} bytes ignored)",
                    file=sys.stderr,
                )
            run_records += trace.records
    except TraceFormatError as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 1
    except MaskUnavailable as exc:
        print(f"errand-trace: {exc}", file=sys.stderr)
        return 3
    header, totals = header_record(thread, run_records), totals_record(thread, run_records)
    ExportHeader.model_validate(header)
    ConversationTotals.model_validate(totals)
    records = [header, *run_records, totals]
    lines = [json.dumps(r, ensure_ascii=True, separators=(",", ":")) for r in records]
    text = "\n".join(lines) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"{out}: {len(lines) - 2} records from {len(paths)} runs", file=sys.stderr)
    return 0


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
    stats_p = sub.add_parser("stats", help="p50 and p95 latency and cost of planning turns")
    stats_p.add_argument("--data-dir", type=Path, default=None)
    stats_p.add_argument("--mode", default="live", help="live (default: live and record), or all")
    export_p = sub.add_parser("export", help="write every run of a conversation as one JSONL")
    export_p.add_argument("--thread", required=True, help="the conversation id")
    export_p.add_argument("--out", type=Path, default=None, help="default: standard output")
    export_p.add_argument(
        "--data-dir", type=Path, default=None, help="default: ERRAND_DATA_DIR or ./data"
    )
    excerpt_p = sub.add_parser("excerpt", help="the design note's excerpt from an exported file")
    excerpt_p.add_argument("file", type=Path, help="an `export` file")
    excerpt_p.add_argument("--kind", required=True, choices=["timeout"])
    args = parser.parse_args(argv)
    if args.command == "excerpt":
        return excerpt_file(args.file, args.kind)
    data_dir = args.data_dir or PathSettings().data_dir
    if args.command == "stats":
        from errand.trace.stats import run as run_stats

        return run_stats(data_dir, args.mode)
    if args.command == "show":
        return show(args.target, data_dir)
    if args.command == "export":
        return export_thread(args.thread, data_dir, args.out)
    parser.error(f"unknown command {args.command}")  # pragma: no cover - argparse exits first
    return 2


if __name__ == "__main__":
    sys.exit(main())
