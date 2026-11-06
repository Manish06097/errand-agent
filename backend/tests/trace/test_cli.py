"""errand-trace show, run as the installed console script in a real subprocess."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.tracer import Tracer
from errand.usage import Usage

SCRIPT = Path(sys.executable).parent / "errand-trace"


def _make_trace(data_dir: Path) -> Path:
    sink = JsonlSink.for_run(data_dir / "traces", "c_01", "r_0001")
    tracer = Tracer(run_id="r_0001", thread_id="c_01", sinks=[sink])
    tracer.start_run(trigger="user_message")
    with tracer.node("plan"):
        tracer.model_call(
            model="claude-opus-5-5",
            usage=Usage(input_uncached=11, cache_write_5m=1843, output=5),
            cost=Decimal("0.009359"),
            started=0.0,
            first_token=0.2,
            ended=1.25,
            mode="scripted",
            effort="medium",
            thinking_display="summarized",
            request_sha256="ab" * 32,
            request={},
            response={
                "text": "",
                "tool_calls": [{"id": "t1", "name": "search_providers", "args": {}}],
            },
            stop_reason="tool_use",
            est_request_tokens=1900,
            floor_tokens=0,
            budget_tokens=12_000,
        )
        tracer.tool_call(tool="search_providers", caller="planner", args={"service": "plumber"})
        tracer.emit(
            "state.transition", **{"from": "intake", "to": "comparing", "reason": "search_done"}
        )
    tracer.end_run(outcome="ANSWERED")
    tracer.close()
    return sink.path


def _show(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    return subprocess.run(
        [str(SCRIPT), "show", *args], capture_output=True, text=True, timeout=60, env=env
    )


def test_the_console_script_is_installed() -> None:
    assert SCRIPT.is_file(), "run uv sync: errand-trace is a [project.scripts] entry"
    assert shutil.which("errand-trace", path=str(SCRIPT.parent)) is not None


def test_show_renders_a_timeline_by_path_and_by_run_id(tmp_path: Path) -> None:
    path = _make_trace(tmp_path)
    by_path = _show(str(path))
    assert by_path.returncode == 0, by_path.stderr
    out = by_path.stdout
    assert "run r_0001  thread c_01" in out
    assert "model.call  claude-opus-5-5 in=11+r0+w1843 out=5 $0.009359 1250ms stop=tool_use" in out
    assert "calls=search_providers" in out
    assert "tool.call  search_providers by planner attempt=1 ok" in out
    assert "intake -> comparing (search_done)" in out
    assert "run.end  ANSWERED model_calls=1 tool_calls=1 tokens=1854/5 $0.009359" in out
    # Children are indented under their node span.
    node_line = next(ln for ln in out.splitlines() if "node.start" in ln)
    call_line = next(ln for ln in out.splitlines() if "model.call" in ln)
    assert call_line.index("model.call") > node_line.index("node.start")
    assert by_path.stderr == ""

    by_id = _show("r_0001", "--data-dir", str(tmp_path))
    assert by_id.returncode == 0, by_id.stderr
    assert by_id.stdout == out


def test_show_tolerates_a_truncated_last_line(tmp_path: Path) -> None:
    path = _make_trace(tmp_path)
    data = path.read_bytes()
    complete = data.count(b"\n")
    path.write_bytes(data[:-40])  # cut into the last record (run.end)
    result = _show(str(path))
    assert result.returncode == 0, result.stderr
    assert "truncated" in result.stderr
    assert "run.end" not in result.stdout
    assert f"({complete - 1} records)" in result.stdout


def test_show_rejects_a_corrupt_line_in_the_middle(tmp_path: Path) -> None:
    path = _make_trace(tmp_path)
    lines = path.read_text().splitlines(keepends=True)
    lines[2] = '{"seq": 3, "type": "node.st\n'
    path.write_text("".join(lines))
    result = _show(str(path))
    assert result.returncode == 1
    assert f"{path}:3: not a JSON record" in result.stderr


@pytest.mark.parametrize("target", ["r_missing", "does/not/exist.jsonl"])
def test_show_says_when_there_is_nothing_to_show(tmp_path: Path, target: str) -> None:
    result = _show(target, "--data-dir", str(tmp_path))
    assert result.returncode == 2
    assert "no single trace found" in result.stderr
