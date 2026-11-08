"""NullSink: the default when no optional sink is configured.

It is both a per-run sink and the process-level sink `make_sink()` returns, so the API
treats an enabled and a disabled Langfuse the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class NullSink:
    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    def write(self, record: Mapping[str, Any], line: str) -> None:
        return None

    def close(self) -> None:
        return None

    def for_run(self, run_id: str, thread_id: str) -> NullSink:
        return self

    def status(self) -> dict[str, Any]:
        return {"state": "off", "reason": self.reason}

    def shutdown(self) -> None:
        return None
