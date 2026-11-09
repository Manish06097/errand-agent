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


class MaskUnavailable(RuntimeError):
    """Hashes exist but cannot be applied; the read is refused rather than served raw."""


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
    """The masker for a data directory.

    No agent.sqlite, a database from before the memory tables, or no hashes in it: there is
    nothing to mask, so it masks nothing. Hashes present but the database unreadable, or
    the key missing, malformed or the wrong size: raise `MaskUnavailable`, because serving
    the raw trace would print exactly the values the user forgot.
    """
    db_path, key_path = data_dir / "agent.sqlite", data_dir / key_file
    if not db_path.is_file():
        return Masker()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'memory_surface_hashes'"
            ).fetchone()
            rows = (
                conn.execute("SELECT hmac, memory_id FROM memory_surface_hashes").fetchall()
                if has_table
                else []
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise MaskUnavailable(f"cannot read {db_path} to find forgotten values: {exc}") from exc
    if not rows:
        return Masker()
    try:
        key = bytes.fromhex(key_path.read_text("ascii").strip())
    except (OSError, ValueError) as exc:
        raise MaskUnavailable(
            f"forgotten values are recorded in {db_path} but {key_path} cannot be read "
            f"({exc.__class__.__name__}); refusing to show an unmasked trace"
        ) from exc
    if len(key) != KEY_BYTES:
        raise MaskUnavailable(
            f"{key_path} holds {len(key)} bytes, expected {KEY_BYTES}; "
            "refusing to show an unmasked trace"
        )
    return Masker(key, {str(h): str(m) for h, m in rows})
