"""JsonlSink: data/traces/{thread_id}/{run_id}.jsonl, the trace of record.

One os.write per line under a lock, so concurrent writers never interleave partial
lines. Records of type approval.*, intent.* and run.end are fsynced before write()
returns. That is all this claims: the bytes were handed to the disk before the caller
moved on to the side effect the record describes.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from errand.trace.schema import needs_fsync

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def trace_path(traces_dir: Path, thread_id: str, run_id: str) -> Path:
    for name, value in (("thread_id", thread_id), ("run_id", run_id)):
        if not _SAFE_ID.match(value) or ".." in value:
            raise ValueError(f"{name} {value!r} is not safe to use in a trace path")
    return traces_dir / thread_id / f"{run_id}.jsonl"


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fd: int | None = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)

    @classmethod
    def for_run(cls, traces_dir: Path, thread_id: str, run_id: str) -> JsonlSink:
        return cls(trace_path(traces_dir, thread_id, run_id))

    def write(self, record: Mapping[str, Any], line: str) -> None:
        data = (line + "\n").encode("utf-8")
        with self._lock:
            if self._fd is None:
                raise RuntimeError(f"JsonlSink for {self.path} is closed")
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
            if needs_fsync(str(record["type"])):
                os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
