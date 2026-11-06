"""NullSink: the default when no optional sink is configured."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class NullSink:
    def write(self, record: Mapping[str, Any], line: str) -> None:
        return None

    def close(self) -> None:
        return None
