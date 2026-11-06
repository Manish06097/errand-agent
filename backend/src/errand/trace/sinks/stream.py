"""StreamSink: hands each record to a writer callable.

The writer is the run's `RunStream.publish_record` (errand.api.sse), owned by the
RunManager, which feeds the SSE stream and its replay. It is not LangGraph's
`get_stream_writer()`: `run.start` and `run.end` are emitted outside any graph node,
where no stream writer exists. The writer gets the very dict the JSONL line was
serialized from, after the JSONL write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class StreamSink:
    def __init__(self, writer: Callable[[Mapping[str, Any]], None]) -> None:
        self._writer = writer

    def write(self, record: Mapping[str, Any], line: str) -> None:
        self._writer(record)

    def close(self) -> None:
        return None
