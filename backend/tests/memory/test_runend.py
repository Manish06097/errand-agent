"""The run-end hook marks as used the rows that were in a request the run actually sent."""

from __future__ import annotations

import json
from pathlib import Path

from errand.agent.memory_runend import sent_memory_ids


def record(sent: bool, ids: list[str]) -> str:
    manifest = {
        "sent": sent,
        "memory_included": [{"id": i, "score": 1.0, "prefill": None} for i in ids],
    }
    return json.dumps({"type": "context.built", "manifest": manifest})


def test_only_sent_requests_count_and_ids_are_deduplicated(tmp_path: Path) -> None:
    trace = tmp_path / "r_1.jsonl"
    trace.write_text(
        "\n".join(
            [
                record(True, ["m-01", "m-02"]),
                record(False, ["m-09"]),  # built, refused by the budget: never sent
                record(True, ["m-02", "m-03"]),
                json.dumps({"type": "model.call", "request": {"memory_included": ["m-77"]}}),
            ]
        ),
        encoding="utf-8",
    )
    assert sent_memory_ids(trace) == ["m-01", "m-02", "m-03"]


def test_a_missing_trace_marks_nothing(tmp_path: Path) -> None:
    assert sent_memory_ids(tmp_path / "nope.jsonl") == []
