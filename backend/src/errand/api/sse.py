"""Run event streams: a broadcaster per live run, and SSE replay from the trace file.

The tracer's `StreamSink` publishes every record to the run's `RunStream` as it is
written (after the JSONL line), and the model gateway's token callback publishes
`token` events. `run.start` and `run.end` are emitted by the RunManager outside any
graph node, which is why the stream is ours and not LangGraph's stream writer.

Replay (`stream_events`): every replayable event is a trace record and carries
`id: <seq>`. A reader subscribes to the live stream first, then reads the run's JSONL
after `Last-Event-ID`, then tails the subscription, dropping anything at or below the
last seq it sent, so no record falls into the gap between file and stream and none is
sent twice. `token` events have no id and are never replayed. The stream ends after
`run.finished`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.sse import ServerSentEvent

from errand.trace.masked import Masker
from errand.trace.reader import read_trace

EVENT_NAMES = {
    "run.start": "run.started",
    "run.end": "run.finished",
    "card": "card",
    "card.update": "card.update",
}


@dataclass(frozen=True)
class _Item:
    record: Mapping[str, Any] | None = None
    token: str | None = None


class RunStream:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[_Item | None]] = set()
        self.closed = False

    def subscribe(self) -> asyncio.Queue[_Item | None]:
        queue: asyncio.Queue[_Item | None] = asyncio.Queue()
        if self.closed:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[_Item | None]) -> None:
        self._subscribers.discard(queue)

    def publish_record(self, record: Mapping[str, Any]) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(_Item(record=dict(record)))

    def publish_token(self, text: str) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(_Item(token=text))

    def close(self) -> None:
        self.closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)
        self._subscribers.clear()


class Broadcaster:
    """Live streams by run id. A stream exists from admission until the run ends."""

    def __init__(self) -> None:
        self._streams: dict[str, RunStream] = {}

    def open(self, run_id: str) -> RunStream:
        stream = RunStream()
        self._streams[run_id] = stream
        return stream

    def get(self, run_id: str) -> RunStream | None:
        return self._streams.get(run_id)

    def finish(self, run_id: str) -> None:
        stream = self._streams.pop(run_id, None)
        if stream is not None:
            stream.close()


def record_event(record: Mapping[str, Any]) -> ServerSentEvent:
    name = EVENT_NAMES.get(str(record["type"]), "trace")
    return ServerSentEvent(data=dict(record), event=name, id=str(record["seq"]))


def token_event(text: str) -> ServerSentEvent:
    return ServerSentEvent(data={"text": text}, event="token")


async def stream_events(
    *, trace_file: Path, stream: RunStream | None, last_event_id: int, masker: Masker | None = None
) -> AsyncIterator[ServerSentEvent]:
    """Replay then follow. A masker hides forgotten values on every record served (gate 2
    decision 14); the trace file itself is never rewritten."""
    mask: Callable[[Any], Any] = masker.record if masker is not None else (lambda r: r)
    queue = stream.subscribe() if stream is not None else None
    try:
        sent = last_event_id
        if trace_file.exists():
            for record in read_trace(trace_file).records:
                record = mask(record)
                seq = int(record["seq"])
                if seq <= sent:
                    continue
                yield record_event(record)
                sent = seq
                if record["type"] == "run.end":
                    return
        if queue is None:
            return
        while True:
            item = await queue.get()
            if item is None:
                return
            if item.token is not None:
                yield token_event(item.token)
                continue
            assert item.record is not None
            seq = int(item.record["seq"])
            if seq <= sent:
                continue
            yield record_event(mask(item.record))
            sent = seq
            if item.record["type"] == "run.end":
                return
    finally:
        if queue is not None and stream is not None:
            stream.unsubscribe(queue)
