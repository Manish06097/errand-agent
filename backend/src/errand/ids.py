"""Ids for conversations and runs: deterministic in sim mode, uuid7 live.

Nothing random or wall-clock based may reach a model request, a replay hash or the
eval table, so evals mint `c_0001`, `r_0001`, ... from counters. The counters start
past the highest id agent.sqlite already holds (not its row count: a minted id that
was never inserted leaves a gap), so a restarted process never reuses an id. Sim ids
assume one API process per data directory; two would mint the same ids.

Every other id is derived from these two and a stable key, never minted: a message is
`<run_id>-u` or `<run_id>-<kind>`, an errand is `e_<run_id>`. That keeps nodes free of
side effects a re-run would repeat (a node re-runs from its first line after a crash).
"""

from __future__ import annotations

import os
import time
from typing import Literal, Protocol

IdMode = Literal["sim", "live"]


class IdFactory(Protocol):
    def conversation_id(self) -> str: ...

    def run_id(self) -> str: ...


class SequentialIds:
    def __init__(self, *, conversations: int = 0, runs: int = 0) -> None:
        self._conversations = conversations
        self._runs = runs

    def conversation_id(self) -> str:
        self._conversations += 1
        return f"c_{self._conversations:04d}"

    def run_id(self) -> str:
        self._runs += 1
        return f"r_{self._runs:04d}"


def uuid7_hex() -> str:
    """RFC 9562 version 7: 48-bit Unix ms, version, 74 random bits, variant."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (rand >> 68) << 64  # 12 bits of rand_a
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return f"{value:032x}"


class Uuid7Ids:
    def conversation_id(self) -> str:
        return f"c_{uuid7_hex()}"

    def run_id(self) -> str:
        return f"r_{uuid7_hex()}"


def message_id_for_user(run_id: str) -> str:
    return f"{run_id}-u"


def message_id_for(run_id: str, kind: str) -> str:
    return f"{run_id}-{kind}"


def errand_id_for(run_id: str) -> str:
    return f"e_{run_id}"
