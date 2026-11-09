"""`errand-trace excerpt <file> --kind timeout`: the design note's trace excerpt (16.3).

Cut from a conversation-level export: one planner `model.call` with its usage and cost,
then the approval-to-verified block of the run that booked. At most MAX_LINES lines, a
record per line, each cut to the keys the reader needs and with long strings shortened.
The same file always gives the same lines. A file with no timeout-after-success
sequence is an error, not an empty excerpt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

MAX_LINES = 12
CUT = 14  # characters kept of an id, fingerprint or key longer than this, plus ".."

Record = Mapping[str, Any]


class ExcerptError(ValueError):
    pass


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > CUT + 4:
        return value[:CUT] + ".."
    if isinstance(value, dict):
        return {k: _short(v) for k, v in value.items()}
    return value


def _round(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


def _line(record: Record, keys: Sequence[str], **extra: Any) -> str:
    out: dict[str, Any] = {"seq": record["seq"], "type": record["type"]}
    for key in keys:
        value = record.get(key)
        if value is not None:
            out[key] = _short(_round(value))
    out.update({k: _short(_round(v)) for k, v in extra.items() if v is not None})
    return json.dumps(out, separators=(",", ":"), ensure_ascii=True)


def _need(found: Record | None, what: str) -> Record:
    if found is None:
        raise ExcerptError(f"no timeout-after-success sequence: {what} is missing")
    return found


def timeout_excerpt(records: Sequence[Record]) -> list[str]:
    """The excerpt lines for a booking whose `book` timed out and was then found confirmed."""
    run_ids: list[str] = []
    for r in records:
        is_book = r.get("type") == "tool.call" and r.get("tool") == "book"
        if is_book and "timeout" in str(r.get("error") or "").lower():
            run_ids.append(str(r.get("run_id")))
    if not run_ids:
        raise ExcerptError("no timeout-after-success sequence: no `book` call ended in a timeout")
    run_id = run_ids[0]
    block = [r for r in records if r.get("run_id") == run_id]

    def first(type_: str, **match: Any) -> Record | None:
        for r in block:
            if r.get("type") == type_ and all(r.get(k) == v for k, v in match.items()):
                return r
        return None

    granted = _need(first("approval.granted"), "approval.granted")
    consumed = _need(first("approval.consumed"), "approval.consumed")
    written = _need(first("intent.written"), "intent.written")
    book = _need(first("tool.call", tool="book"), "the book call")
    status = _need(first("tool.call", tool="get_booking_status"), "the get_booking_status call")
    resolved = _need(first("intent.resolved"), "intent.resolved")
    end = _need(first("run.end"), "run.end")
    transition = first("state.transition", to="verifying")
    result = status.get("result") or {}
    if result.get("status") != "confirmed" or resolved.get("state") != "succeeded":
        raise ExcerptError("no timeout-after-success sequence: the status check did not confirm")

    planner = [r for r in records if r.get("type") == "model.call" and not r.get("error")]
    before = [r for r in planner if _before(records, r, granted)]
    call = _need(before[-1] if before else None, "a planner model.call before the grant")

    totals = end.get("totals") or {}
    lines = [
        _line(
            call, ("run_id", "model", "effort", "usage", "cost_usd", "latency_ms", "stop_reason")
        ),
        "...",
        _line(granted, ("approval_id", "fingerprint", "via")),
        _line(consumed, ("approval_id", "intent_id")),
        _line(written, ("intent_id", "attempt", "idempotency_key", "hold_id")),
        _line(book, ("tool", "args", "error", "latency_ms")),
    ]
    if transition is not None:
        lines.append(_line(transition, ("from", "to", "reason")))
    lines += [
        _line(
            status,
            ("tool", "args"),
            result={k: result.get(k) for k in ("status", "booking_id")},
        ),
        _line(resolved, ("intent_id", "state", "booking_id", "book_calls")),
        _line(
            end,
            ("outcome",),
            totals={k: totals.get(k) for k in ("model_calls", "tool_calls", "cost_usd")},
        ),
    ]
    assert len(lines) <= MAX_LINES
    return lines


def _before(records: Sequence[Record], call: Record, marker: Record) -> bool:
    """Whether `call` is earlier in the file than `marker` (file order is run order)."""
    for r in records:
        if r is call:
            return True
        if r is marker:
            return False
    return False


def excerpt(records: Sequence[Record], kind: str) -> list[str]:
    if kind == "timeout":
        return timeout_excerpt(records)
    raise ExcerptError(f"unknown excerpt kind {kind!r}")
