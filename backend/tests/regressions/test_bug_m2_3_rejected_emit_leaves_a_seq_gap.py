"""BUG-M2-3: a rejected trace record still uses up a seq number and a span id.

Tracer.emit advanced `seq` and the span counter before validating the record against
schema v1. When validation failed (a missing required field, an unknown field) nothing
was written, but the counters had moved, so the trace of record read seq [1, 3]. A
reader takes that gap as a lost line, and checks that expect contiguous seq would flag
a run where nothing was lost. A node whose start record was rejected also bumped that
node's attempt number.

The repro drives a real Tracer with a JsonlSink and a StreamSink. It interleaves good
records with one rejected record of each kind (an invalid run start, an invalid emit, a
node whose start record is invalid) and reads both sinks back.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from errand.trace.reader import read_trace
from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.sinks.stream import StreamSink
from errand.trace.tracer import Tracer


def test_rejected_records_leave_seq_contiguous_and_span_ids_dense(tmp_path: Path) -> None:
    streamed: list[Mapping[str, Any]] = []
    jsonl = JsonlSink.for_run(tmp_path, "c_01", "run-0001")
    tracer = Tracer(run_id="run-0001", thread_id="c_01", sinks=[jsonl, StreamSink(streamed.append)])

    with pytest.raises(ValidationError):
        tracer.start_run(trigger="user_message", favourite_colour="blue")
    tracer.start_run(trigger="user_message")
    with pytest.raises(ValidationError):
        tracer.emit("tool.call", tool="search_providers")  # caller and args are required
    with pytest.raises(ValidationError), tracer.node("plan", favourite_colour="blue"):
        pass
    tracer.tool_call(tool="search_providers", caller="planner", args={})
    with tracer.node("plan"):
        tracer.tool_call(tool="get_provider", caller="planner", args={"provider_id": "P1"})
    tracer.end_run(outcome="ANSWERED")
    tracer.close()

    records = read_trace(jsonl.path).records
    assert records == list(streamed)
    shape = [
        (r["seq"], r["type"], r["span_id"], r["parent_span_id"], r["node_attempt"]) for r in records
    ]
    # The rejected node never started, so the plan node that did is attempt 1.
    assert shape == [
        (1, "run.start", "s_1", None, 1),
        (2, "tool.call", "s_2", "s_1", 1),
        (3, "node.start", "s_3", "s_1", 1),
        (4, "tool.call", "s_4", "s_3", 1),
        (5, "node.end", "s_3", "s_1", 1),
        (6, "run.end", "s_1", None, 1),
    ]
