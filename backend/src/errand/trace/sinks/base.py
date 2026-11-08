"""The sink protocols and the single serialization every sink shares."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class Sink(Protocol):
    def write(self, record: Mapping[str, Any], line: str) -> None:
        """`line` is `record` serialized once, without the trailing newline."""

    def close(self) -> None: ...


@runtime_checkable
class LiveSink(Protocol):
    """Optional: a sink that shows a call while it runs, not only once its record exists.

    `model.call` and `tool.call` are written after the call finishes, and Langfuse v5
    cannot backdate a start time (spike F3), so a sink that only saw the record would
    show every call as 0 ms. `Tracer.opening()` calls `begin` before the call, then
    `bind` with the span id of the record the call produced, just before that record
    reaches `write`, or `abandon` if the call ended without one.
    """

    def begin(
        self, open_id: str, run_id: str, parent_span_id: str | None, kind: str, name: str
    ) -> None: ...

    def bind(self, open_id: str, span_id: str) -> None: ...

    def abandon(self, open_id: str, error: str) -> None: ...


class ProcessSink(Protocol):
    """The optional dashboard sink, built once per process by `make_sink()`.

    The API lifespan owns it: each run gets its own view through `for_run`, which goes
    into that run's Tracer after the JSONL and stream sinks, and the view's `close()`
    only forgets that run. `shutdown()` is for the lifespan exit and nothing else.
    """

    def for_run(self, run_id: str, thread_id: str) -> Sink: ...

    def status(self) -> dict[str, Any]:
        """For /api/health. Must not block or touch the network."""

    def shutdown(self) -> None: ...


def encode_record(model: BaseModel) -> tuple[dict[str, Any], str]:
    record = model.model_dump(mode="json", by_alias=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return record, line
