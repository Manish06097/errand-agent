"""The run-end hook: suggestions after a completed errand, and `last_used_at` (decisions 4, 16).

Called by the RunManager once the run's own writes are committed and before its single
`run.end`. Two jobs:

1. If the errand completed (a verified booking, cancel or reschedule), run the two
   inference rules over the user's booking records. Each new pending row becomes one
   `MemorySuggestionChip` in the conversation, after the outcome message.
2. `mark_used` for the active rows that were in a request this run actually sent, read from
   its `context.built` records with `sent: true` (what was selected is not what was sent).

A failure in either is logged as an `error` trace record and never changes the run's outcome
or its one `run.end` (INV-07).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from errand.agent.deps import RunDeps
from errand.agent.memory_tools import post_chip
from errand.domain.messages import MemorySuggestionChip
from errand.domain.outcomes import RunOutcome
from errand.trace.tracer import Tracer

log = logging.getLogger(__name__)

COMPLETED = frozenset(
    {
        RunOutcome.BOOKED_VERIFIED,
        RunOutcome.CANCELLED_VERIFIED,
        RunOutcome.RESCHEDULED_VERIFIED,
    }
)


def sent_memory_ids(trace_file: Path) -> list[str]:
    """Memory ids in any request this run sent, in first-seen order."""
    seen: dict[str, None] = {}
    try:
        lines = trace_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if '"context.built"' not in line:
            continue
        record = json.loads(line)
        manifest = record.get("manifest") or {}
        if record.get("type") != "context.built" or manifest.get("sent") is not True:
            continue
        for item in manifest.get("memory_included") or []:
            seen[str(item["id"])] = None
    return list(seen)


async def run_end(
    deps: RunDeps | None, tracer: Tracer, outcome: RunOutcome, trace_file: Path, turn_no: int | None
) -> None:
    if deps is None or deps.memory is None:
        return
    memory = deps.memory
    if outcome in COMPLETED:
        try:
            written: list[dict[str, Any]] = await memory.run_inference(
                user_id=deps.user_id, tz=deps.tz, run_id=deps.run_id
            )
            for row in written:
                deps.chip_seq += 1
                tracer.emit(
                    "memory.written",
                    memory_id=row["memory_id"],
                    kind=row["kind"],
                    status=row["status"],
                    value_hash=memory.hash_value(row["value"]),
                )
                await post_chip(
                    deps,
                    MemorySuggestionChip(memory_id=row["memory_id"]),
                    turn_no=turn_no,
                    seq=deps.chip_seq,
                )
        except Exception as exc:
            _failed(tracer, "memory.run_inference", exc)
    try:
        ids = sent_memory_ids(trace_file)
        if ids:
            await memory.mark_used(deps.user_id, ids)
            for memory_id in ids:
                tracer.emit("memory.used", memory_id=memory_id)
    except Exception as exc:
        _failed(tracer, "memory.mark_used", exc)


def _failed(tracer: Tracer, where: str, exc: Exception) -> None:
    log.exception("run-end memory hook failed: %s", where)
    try:
        tracer.emit(
            "error", where=where, exception_type=type(exc).__name__, message=str(exc)[:2000]
        )
    except Exception:
        log.exception("could not trace the failure of %s", where)
