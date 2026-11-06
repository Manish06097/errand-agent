"""StreamSink: hands each record to a writer callable.

In M3 the writer is LangGraph's `get_stream_writer()`, which feeds the SSE trace panel.
The writer gets the very dict the JSONL line was serialized from.
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
