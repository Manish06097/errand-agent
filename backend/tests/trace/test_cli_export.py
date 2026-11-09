"""`errand-trace export --thread`: every run of a conversation, in order, as one JSONL.

In process (no subprocess): the CLI's `main` over traces a real S22 run wrote, so the
export is checked against what the product produces, and `show` must read it back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from errand.trace import cli
from errand.trace.reader import read_trace
from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario


async def _s22_root(tmp_path: Path) -> tuple[Path, str]:
    scenario = load_scenario(EVALS / "scenarios" / "S22_timeout_after_success.yaml")
    root = tmp_path / "s22"
    root.mkdir()
    result = await run_scenario(scenario, root)
    assert result.passed, result.failures
    (thread,) = [p.name for p in (root / "traces").iterdir()]
    return root, thread


async def test_export_writes_every_run_of_the_thread_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, thread = await _s22_root(tmp_path)
    out = tmp_path / "timeout_after_success.jsonl"
    assert cli.main(["export", "--thread", thread, "--out", str(out), "--data-dir", str(root)]) == 0
    assert "records from 2 runs" in capsys.readouterr().err
    exported = read_trace(out).records
    assert read_trace(out).truncated_tail is None
    # the header first, the conversation totals last, the runs between them as written
    assert (exported[0]["type"], exported[-1]["type"]) == ("export.header", "conversation.totals")
    records = exported[1:-1]
    runs = [r["run_id"] for r in records]
    assert runs == sorted(runs) and set(runs) == {"r_0001", "r_0002"}
    assert [r["type"] for r in records if r["type"] == "run.end"] == ["run.end", "run.end"]
    # the booking run, in the order the world saw it: book times out, then the status check
    tools = [r["tool"] for r in records if r["type"] == "tool.call" and r["run_id"] == "r_0002"]
    assert tools.index("book") < tools.index("get_booking_status")
    source = [
        r
        for run in sorted((root / "traces" / thread).glob("*.jsonl"))
        for r in read_trace(run).records
    ]
    assert records == source


async def test_export_goes_to_stdout_and_escapes_non_ascii(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, thread = await _s22_root(tmp_path)
    assert cli.main(["export", "--thread", thread, "--data-dir", str(root)]) == 0
    text = capsys.readouterr().out
    assert text.isascii() and "\\u20b9" in text  # the rupee sign, escaped
    assert all(json.loads(line)["thread_id"] == thread for line in text.splitlines())
    assert text.splitlines()[0].startswith('{"v":1,"type":"export.header"')


async def test_show_reads_the_export_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, thread = await _s22_root(tmp_path)
    out = tmp_path / "export.jsonl"
    cli.main(["export", "--thread", thread, "--out", str(out), "--data-dir", str(root)])
    capsys.readouterr()
    assert cli.main(["show", str(out)]) == 0
    shown = capsys.readouterr()
    assert shown.err == "" and "run.end  BOOKED_VERIFIED" in shown.out


def test_export_says_when_the_thread_has_no_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["export", "--thread", "c_none", "--data-dir", str(tmp_path)]) == 2
    assert "no runs found for thread 'c_none'" in capsys.readouterr().err


def test_export_refuses_a_corrupt_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "traces" / "c_01"
    folder.mkdir(parents=True)
    (folder / "r_0001.jsonl").write_text('{"seq": 1}\nnot json\n')
    assert cli.main(["export", "--thread", "c_01", "--data-dir", str(tmp_path)]) == 1
    assert "not a JSON record" in capsys.readouterr().err
