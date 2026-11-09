"""Masking forgotten values on read (gate 2 decision 14, architecture 12.3 step 7).

The JSONL trace is append-only and its `model.call` records hold what was sent, so a
value forgotten later can still sit in an old file. The file is never rewritten (that
would break the request hashes the evals read). Instead every user-facing read applies
the hashes the forget wrote: any run of words whose normalised HMAC matches a forgotten
surface form or redacted span is replaced by a marker.

Structural fields (ids, timestamps, hashes) are left alone so a masked trace still lines
up with the raw one record for record.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errand.memory.hmac_key import KEY_BYTES
from errand.memory.mask import MaskResult, mask_text

_STRUCTURAL = frozenset(
    {
        "type",
        "ts",
        "wall_ts",
        "run_id",
        "thread_id",
        "errand_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "request_sha256",
        "signature_hash",
        "reviews_sha256",
        "memory_id",
        "message_id",
        "approval_id",
        "idempotency_key",
        "fingerprint",
    }
)


@dataclass
class Masker:
    key: bytes = b""
    hashes: Mapping[str, str] = field(default_factory=dict)
    masked: int = 0

    @property
    def active(self) -> bool:
        return bool(self.hashes) and len(self.key) == KEY_BYTES

    def text(self, value: str) -> str:
        if not self.active:
            return value
        result: MaskResult = mask_text(value, self.key, self.hashes)
        self.masked += result.masked
        return result.text

    def value(self, value: Any, name: str = "") -> Any:
        if name in _STRUCTURAL or name.endswith("_sha256") or name.endswith("_id"):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        return value

    def record(self, record: dict[str, Any]) -> dict[str, Any]:
        if not self.active:
            return record
        masked: dict[str, Any] = self.value(record)
        return masked


def masker_for(data_dir: Path, key_file: str = "memory_hmac.key") -> Masker:
    """The masker for a data directory: its hashes and key, or one that masks nothing."""
    db_path, key_path = data_dir / "agent.sqlite", data_dir / key_file
    if not db_path.is_file() or not key_path.is_file():
        return Masker()
    try:
        key = bytes.fromhex(key_path.read_text("ascii").strip())
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT hmac, memory_id FROM memory_surface_hashes").fetchall()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error):
        return Masker()
    return Masker(key, {str(h): str(m) for h, m in rows})
