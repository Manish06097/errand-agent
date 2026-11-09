"""Read a JSONL trace back, tolerating a torn last line.

The writer emits each record and its newline in a single write, so a file that does
not end in a newline was cut off mid-record (a crash, or a copy taken while a run was
still writing). That last fragment is reported, not parsed. A bad line anywhere else
is corruption and raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TraceFormatError(ValueError):
    pass


@dataclass(frozen=True)
class TraceFile:
    path: Path
    records: list[dict[str, Any]]
    truncated_tail: bytes | None


def read_trace(path: Path) -> TraceFile:
    data = path.read_bytes()
    lines = data.split(b"\n")
    tail = lines.pop()  # b"" when the file ends with a newline
    records: list[dict[str, Any]] = []
    for lineno, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise TraceFormatError(f"{path}:{lineno}: not a JSON record ({exc})") from None
        if not isinstance(record, dict):
            raise TraceFormatError(f"{path}:{lineno}: a record must be a JSON object")
        records.append(record)
    return TraceFile(path, records, tail or None)


def to_jsonl(trace: TraceFile) -> bytes:
    """The records of a read trace as JSONL, one line each, same shape the writer emits.

    A download is built from the records `read_trace` returned, not from the file's
    bytes, so whatever the reader masks (M11's forget mask) is masked here too.
    """
    lines = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in trace.records
    )
    return b"".join(line.encode("utf-8") + b"\n" for line in lines)
