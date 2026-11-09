"""The conversation-level export (16.3): its header, its totals, its masking and the
excerpt cut from it, checked on a real S22 run (scripted), not on hand-built dicts."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from errand.memory import hmac_key
from errand.memory.mask import hash_of
from errand.trace import cli
from errand.trace.excerpt import MAX_LINES
from errand.trace.reader import read_trace
from errand.trace.schema import EXPORT_ADAPTER
from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario
from tests.hygiene.test_design_note import add_to, run_check

pytestmark = pytest.mark.slow  # the excerpt test runs check_design_note.py as make check does


async def _run(name: str, root: Path) -> str:
    result = await run_scenario(load_scenario(EVALS / "scenarios" / f"{name}.yaml"), root)
    assert result.passed, result.failures
    (thread,) = [p.name for p in (root / "traces").iterdir()]
    return thread


async def _export(name: str, tmp_path: Path) -> tuple[Path, str, Path]:
    root = tmp_path / name
    root.mkdir()
    thread = await _run(name, root)
    out = tmp_path / f"{name}.jsonl"
    assert cli.main(["export", "--thread", thread, "--out", str(out), "--data-dir", str(root)]) == 0
    return root, thread, out


async def test_header_names_the_runs_the_mode_and_where_the_sha_came_from(tmp_path: Path) -> None:
    _, thread, out = await _export("S22_timeout_after_success", tmp_path)
    header = read_trace(out).records[0]
    assert header["type"] == "export.header" and header["thread_id"] == thread
    assert header["run_ids"] == ["r_0001", "r_0002"]
    assert header["model_mode"] == "scripted" and header["clock_mode"] == "sim"
    # run.start does not record a SHA, so the exporting checkout's is used and says so
    assert header["git_sha_source"] == "exporting_checkout"
    assert header["git_sha"] is None or len(header["git_sha"]) == 40


async def test_every_exported_record_validates_against_schema_v1(tmp_path: Path) -> None:
    _, _, out = await _export("S22_timeout_after_success", tmp_path)
    for record in read_trace(out).records:
        EXPORT_ADAPTER.validate_python(record)


async def test_totals_equal_a_hand_sum_of_the_runs(tmp_path: Path) -> None:
    _, _, out = await _export("S22_timeout_after_success", tmp_path)
    records = read_trace(out).records
    totals = records[-1]
    ends = [r for r in records if r["type"] == "run.end"]
    assert len(ends) == 2
    assert totals["runs"] == 2
    assert totals["model_calls"] == sum(e["totals"]["model_calls"] for e in ends) > 0
    assert totals["tool_calls"] == sum(e["totals"]["tool_calls"] for e in ends)
    assert totals["tokens_in"] == sum(e["totals"]["tokens_in"] for e in ends)
    assert totals["tokens_out"] == sum(e["totals"]["tokens_out"] for e in ends)
    assert Decimal(str(totals["cost_usd"])) == sum(
        (Decimal(str(e["totals"]["cost_usd"])) for e in ends), Decimal(0)
    )
    # and from the model.call records themselves, so one record counted twice shows up
    calls = [r for r in records if r["type"] == "model.call"]
    assert totals["model_calls"] == len(calls)
    assert Decimal(str(totals["cost_usd"])) == sum(
        (Decimal(str(c["cost_usd"])) for c in calls), Decimal(0)
    ) + sum(
        (
            Decimal(str(r["cost_usd"]))
            for r in records
            if r["type"] == "reader.call" and r["cost_usd"]
        ),
        Decimal(0),
    )


async def test_a_forgotten_value_is_masked_in_the_export_and_present_in_the_raw_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "forgotten"
    root.mkdir()
    thread = await _run("S22_timeout_after_success", root)
    key = hmac_key.fixed_key("t")
    (root / "memory_hmac.key").write_text(key.hex(), encoding="ascii")
    conn = sqlite3.connect(root / "agent.sqlite")
    conn.execute(
        "INSERT INTO memory_surface_hashes (memory_id, kind, hmac) "
        "VALUES ('m-01', 'surface_form', ?)",
        (hash_of(key, "Indiranagar"),),
    )
    conn.commit()
    conn.close()
    raw = "".join(p.read_text() for p in (root / "traces" / thread).glob("*.jsonl"))
    assert "Indiranagar" in raw
    out = tmp_path / "masked.jsonl"
    assert cli.main(["export", "--thread", thread, "--out", str(out), "--data-dir", str(root)]) == 0
    assert "Indiranagar" not in out.read_text()


async def test_the_excerpt_is_short_deterministic_and_passes_the_design_note_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, out = await _export("S22_timeout_after_success", tmp_path)
    capsys.readouterr()
    assert cli.main(["excerpt", str(out), "--kind", "timeout"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["excerpt", str(out), "--kind", "timeout"]) == 0
    assert capsys.readouterr().out == first
    lines = first.splitlines()
    assert 1 < len(lines) <= MAX_LINES
    types = [json.loads(line)["type"] for line in lines if line != "..."]
    assert types[0] == "model.call"
    assert types[1:4] == ["approval.granted", "approval.consumed", "intent.written"]
    # the timeout, then the status check that found the booking, then the resolution
    book = next(json.loads(x) for x in lines if '"tool":"book"' in x)
    assert "imeout" in book["error"]
    status = next(json.loads(x) for x in lines if '"get_booking_status"' in x)
    assert status["result"]["status"] == "confirmed"
    assert lines.index(json.dumps(book, separators=(",", ":"))) < lines.index(
        next(x for x in lines if '"get_booking_status"' in x)
    )
    assert types[-2:] == ["intent.resolved", "run.end"]
    note = add_to("Trace excerpt", "```\n" + first + "```")
    result = run_check(tmp_path, note)
    assert result.returncode == 0, result.stderr


async def test_the_excerpt_refuses_a_conversation_with_no_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, out = await _export("S01_happy_path_plumber", tmp_path)
    capsys.readouterr()
    assert cli.main(["excerpt", str(out), "--kind", "timeout"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "no timeout-after-success sequence" in captured.err
