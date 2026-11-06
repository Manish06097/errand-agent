"""The sink protocol and the single serialization every sink shares."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel


class Sink(Protocol):
    def write(self, record: Mapping[str, Any], line: str) -> None:
        """`line` is `record` serialized once, without the trailing newline."""

    def close(self) -> None: ...


def encode_record(model: BaseModel) -> tuple[dict[str, Any], str]:
    record = model.model_dump(mode="json", by_alias=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return record, line
