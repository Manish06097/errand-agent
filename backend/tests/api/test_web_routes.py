"""M7's web routes: the conversation list, meta, and the trace routes that read only
through `trace/reader.py` (Gate 2 decision 14)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from errand.trace import reader
from evals.runner import Harness, harness
from tests.api.support import world

API = Path(__file__).resolve().parents[2] / "src" / "errand" / "api"
SAY = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


async def one_run(h: Harness) -> str:
    view = await h.say(SAY)
    return view.run_id


async def test_the_conversation_list_is_newest_first_with_a_preview(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        first = h.conversation_id
        await one_run(h)
        second = await h.switch("second")
        listing = (await h.client.get("/api/conversations")).json()
    assert [c["conversation_id"] for c in listing] == [first, second]
    assert listing[0]["preview"] == SAY[:140]
    assert listing[1]["preview"] is None
    assert listing[0]["active_run_id"] is None


async def test_meta_says_dev_is_off_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERRAND_DEV", raising=False)
    async with harness(tmp_path, world()) as h:
        meta = (await h.client.get("/api/meta")).json()
    assert meta == {"dev": False, "model_mode": "scripted"}


async def test_the_trace_routes_serve_the_run_and_a_jsonl_download(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        run_id = await one_run(h)
        trace = (await h.client.get(f"/api/runs/{run_id}/trace")).json()
        download = await h.client.get(f"/api/runs/{run_id}/trace.jsonl")
        missing = await h.client.get("/api/runs/r_9999/trace")
        missing_dl = await h.client.get("/api/runs/r_9999/trace.jsonl")
    assert trace["run_id"] == run_id and trace["truncated"] is False
    types = [r["type"] for r in trace["records"]]
    assert types[0] == "run.start" and types[-1] == "run.end"
    lines = [json.loads(line) for line in download.text.splitlines()]
    assert lines == trace["records"]
    assert download.headers["content-disposition"] == f'attachment; filename="{run_id}.jsonl"'
    assert missing.status_code == 404 and missing_dl.status_code == 404


async def test_the_trace_routes_read_through_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a route read the file any other way, M11's forget mask would not apply to it."""
    async with harness(tmp_path, world()) as h:
        run_id = await one_run(h)
        real = reader.read_trace
        seen: list[Path] = []

        def masked(path: Path) -> reader.TraceFile:
            seen.append(path)
            trace = real(path)
            records = [{**r, "masked_by_reader": True} for r in trace.records]
            return reader.TraceFile(trace.path, records, trace.truncated_tail)

        monkeypatch.setattr(reader, "read_trace", masked)
        as_json: Any = (await h.client.get(f"/api/runs/{run_id}/trace")).json()
        as_jsonl = (await h.client.get(f"/api/runs/{run_id}/trace.jsonl")).text
    assert len(seen) == 2
    assert all(r["masked_by_reader"] for r in as_json["records"])
    assert all(json.loads(line)["masked_by_reader"] for line in as_jsonl.splitlines())


FILE_READS = {"open", "read_bytes", "read_text"}
# `.open()` on these is not a file: a database handle and the SSE broadcaster.
NOT_FILES = {"AgentDB", "self.broadcaster"}


def file_reads(source: str) -> list[tuple[int, str]]:
    """Calls that open or read a file: `open(...)` and any `x.open()`, `.read_bytes()`,
    `.read_text()`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        receiver = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
        if name in FILE_READS and receiver not in NOT_FILES:
            found.append((node.lineno, name))
    return found


def test_the_static_scan_catches_every_way_to_open_a_file() -> None:
    assert file_reads("open('x')") == [(1, "open")]
    assert file_reads("path.open()") == [(1, "open")]
    assert file_reads("(root / 'x').open('rb')") == [(1, "open")]
    assert file_reads("p.read_bytes()") == [(1, "read_bytes")]
    assert file_reads("p.read_text()") == [(1, "read_text")]
    assert file_reads("p.is_file()") == []


def test_no_api_module_opens_a_file_itself() -> None:
    """A static backstop for the test above: the API package never reads files directly."""
    offenders = [
        f"{path.name}:{line} {name}"
        for path in sorted(API.glob("*.py"))
        for line, name in file_reads(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
