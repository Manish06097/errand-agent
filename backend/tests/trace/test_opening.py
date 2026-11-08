"""Tracer.opening(): begin before the call, bind to its record, abandon without one.

The API-level proof is in test_langfuse_sink.py (real durations in Langfuse); these pin
the protocol itself, which no record in the JSONL shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.tracer import Tracer, UnknownTraceType


class Recorder:
    """A LiveSink that writes down every call it gets."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail

    def write(self, record: Mapping[str, Any], line: str) -> None:
        self.calls.append(("write", record["type"], record["span_id"]))

    def close(self) -> None:
        self.calls.append(("close",))

    def begin(
        self, open_id: str, run_id: str, parent_span_id: str | None, kind: str, name: str
    ) -> None:
        self.calls.append(("begin", open_id, parent_span_id, kind, name))
        if self.fail:
            raise RuntimeError("begin failed")

    def bind(self, open_id: str, span_id: str) -> None:
        self.calls.append(("bind", open_id, span_id))

    def abandon(self, open_id: str, error: str) -> None:
        self.calls.append(("abandon", open_id, error))


def tracer(tmp_path: Path, sink: Any) -> tuple[Tracer, JsonlSink]:
    jsonl = JsonlSink.for_run(tmp_path, "c_01", "r_0001")
    t = Tracer(run_id="r_0001", thread_id="c_01", sinks=[jsonl, sink])
    t.start_run(trigger="user_message")
    return t, jsonl


def test_begin_then_bind_just_before_the_record_reaches_the_sink(tmp_path: Path) -> None:
    rec = Recorder()
    t, jsonl = tracer(tmp_path, rec)
    with t.node("plan"), t.opening("tool.call", "search_providers"):
        t.emit("state.transition", **{"from": "a", "to": "b", "reason": "x"})
        t.tool_call(tool="search_providers", caller="planner", args={})
    assert rec.calls[1:] == [
        ("write", "node.start", "s_2"),
        ("begin", "o_1", "s_2", "tool.call", "search_providers"),
        ("write", "state.transition", "s_3"),
        ("bind", "o_1", "s_4"),
        ("write", "tool.call", "s_4"),
        ("write", "node.end", "s_2"),
    ]
    # opening() itself writes nothing to the JSONL.
    lines = jsonl.path.read_text().splitlines()
    assert len(lines) == 5


def test_an_exception_before_the_record_abandons_with_its_type(tmp_path: Path) -> None:
    rec = Recorder()
    t, _ = tracer(tmp_path, rec)
    with pytest.raises(TimeoutError), t.opening("model.call", "claude-opus-5-5"):
        raise TimeoutError()
    with t.opening("model.call", "claude-opus-5-5"):
        pass
    assert [c for c in rec.calls if c[0] == "abandon"] == [
        ("abandon", "o_1", "TimeoutError"),
        ("abandon", "o_2", "no record"),
    ]


def test_only_the_first_record_of_the_opened_type_binds(tmp_path: Path) -> None:
    rec = Recorder()
    t, _ = tracer(tmp_path, rec)
    with t.opening("tool.call", "book"):
        t.tool_call(tool="book", caller="executor", args={})
        t.tool_call(tool="get_booking_status", caller="executor", args={})
    assert [c for c in rec.calls if c[0] in ("bind", "abandon")] == [("bind", "o_1", "s_2")]


def test_a_failing_live_sink_never_fails_the_call(tmp_path: Path) -> None:
    rec = Recorder(fail=True)
    t, jsonl = tracer(tmp_path, rec)
    with t.opening("tool.call", "book"):
        t.tool_call(tool="book", caller="executor", args={})
    # The sink that failed to begin is not asked to bind; the record still went out.
    assert [c[0] for c in rec.calls] == ["write", "begin", "write"]
    assert len(jsonl.path.read_text().splitlines()) == 2


def test_no_live_sink_means_no_bookkeeping(tmp_path: Path) -> None:
    jsonl = JsonlSink.for_run(tmp_path, "c_01", "r_0001")
    t = Tracer(run_id="r_0001", thread_id="c_01", sinks=[jsonl])
    with t.opening("tool.call", "book"):
        t.tool_call(tool="book", caller="executor", args={})
    with pytest.raises(UnknownTraceType), t.opening("model.done", "x"):
        pass
