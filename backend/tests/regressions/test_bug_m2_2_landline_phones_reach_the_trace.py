"""BUG-M2-2: landline phone numbers and a pincode after a city reach the trace in the clear.

The sanitizer masked phones only by regex, and the regex knew Indian mobiles and
+country numbers. A Bengaluru landline in a `phone` field ("080 4123 4567") or in free
text ("call (080) 4123-4567") went to the JSONL trace of record and to the SSE stream
unchanged. Address masking also stopped at the street, so "12 MG Road, Bengaluru 560038"
became "[address], Bengaluru 560038" and the pincode survived.

The repro drives a real Tracer with a JsonlSink and a StreamSink through a tool.call
shaped like `get_provider` (architecture section 4.2) and reads both sinks back. The
negative half checks that slot timestamps, epoch milliseconds and ids are not mistaken
for phone numbers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from errand.trace.reader import read_trace
from errand.trace.sinks.jsonl import JsonlSink
from errand.trace.sinks.stream import StreamSink
from errand.trace.tracer import Tracer

SLOT_START = "2026-11-07T10:00:00.000+05:30"
SLOT_START_MS = 1794025800000


def _provider_result() -> dict[str, Any]:
    return {
        "provider_id": "P7",
        "name": "Shree Plumbing Works",
        "service": "plumber",
        "area": "Indiranagar",
        "price_inr": 450,
        "rating": 4.6,
        "phone": "080 4123 4567",
        "phone_number": "(080) 4123 4567",
        "contact": {"mobile": "080-41234567", "whatsapp": "08041234567"},
        "attributes": {"verified": True, "languages": ["kn", "en"]},
        "about": "Since 1998. Call (080) 4123-4567, shop at 12 MG Road, Bengaluru 560038",
        "available_slots": [
            {
                "slot_id": "slot_P7_1794025800000",
                "start": SLOT_START,
                "start_ms": SLOT_START_MS,
                "start_ms_text": str(SLOT_START_MS),
            }
        ],
        "reviews": [],
    }


def test_landlines_and_pincodes_reach_neither_the_jsonl_nor_the_stream(tmp_path: Path) -> None:
    streamed: list[Mapping[str, Any]] = []
    jsonl = JsonlSink.for_run(tmp_path, "c_01", "run-0001")
    tracer = Tracer(run_id="run-0001", thread_id="c_01", sinks=[jsonl, StreamSink(streamed.append)])
    tracer.start_run(trigger="user_message")
    tracer.tool_call(
        tool="get_provider", caller="planner", args={"provider_id": "P7"}, result=_provider_result()
    )
    tracer.end_run(outcome="ANSWERED")
    tracer.close()

    for sink, blob in (
        ("jsonl", jsonl.path.read_text(encoding="utf-8")),
        ("stream", json.dumps(streamed, ensure_ascii=False)),
    ):
        for leaked in ("4123", "41234567", "560038", "MG Road"):
            assert leaked not in blob, f"{leaked!r} reached the {sink} sink"

    (record,) = [r for r in read_trace(jsonl.path).records if r["type"] == "tool.call"]
    result = record["result"]
    assert result["phone"] == "[phone]"
    assert result["phone_number"] == "[phone]"
    assert result["contact"] == {"mobile": "[phone]", "whatsapp": "[phone]"}
    assert "[phone]" in result["about"]


def test_slot_timestamps_epoch_ms_and_ids_survive(tmp_path: Path) -> None:
    streamed: list[Mapping[str, Any]] = []
    jsonl = JsonlSink.for_run(tmp_path, "c_01", "run-0001")
    tracer = Tracer(run_id="run-0001", thread_id="c_01", sinks=[jsonl, StreamSink(streamed.append)])
    tracer.tool_call(
        tool="get_provider", caller="planner", args={"provider_id": "P7"}, result=_provider_result()
    )
    (record,) = read_trace(jsonl.path).records
    assert record == streamed[0]
    slot = record["result"]["available_slots"][0]
    assert slot == {
        "slot_id": "slot_P7_1794025800000",
        "start": SLOT_START,
        "start_ms": SLOT_START_MS,
        "start_ms_text": str(SLOT_START_MS),
    }
    assert record["result"]["provider_id"] == "P7"
    assert record["result"]["price_inr"] == 450
