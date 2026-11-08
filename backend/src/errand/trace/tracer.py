"""The Tracer: one per run. Assigns seq and span ids, sanitizes once, fans out.

Order per record: sanitize the payload, build the envelope with candidate seq and span
ids, validate against schema v1, serialize once, commit the counters, then hand the
same dict and line to every sink, JsonlSink first. A rejected record uses up nothing.
A failing JSONL write raises (it is the record of truth); any other sink failing is
logged and skipped.

`opening(record_type, name)` brackets a call whose record is written after it ends
(`model.call`, `tool.call`). It writes nothing to the JSONL; it only lets a `LiveSink`
(Langfuse) start showing the call now and finish that same observation when the record
arrives, so the dashboard shows real durations (spike F3).

`ts` comes from the injected `now` callable, normally a `Clock.now` (simulated time in
tests and evals, `SystemClock` by default); `wall_ts` is always real time from
`errand.clock.wall_now`. Ids are deterministic per run: seq counts from 1, spans are s_1,
s_2, ... in emit order, and the run span is s_1.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from errand.clock import SystemClock, wall_now
from errand.trace.sanitize import sanitize
from errand.trace.schema import RECORD_ADAPTER, RECORD_TYPES, RunTotals
from errand.trace.sinks.base import LiveSink, Sink, encode_record
from errand.usage import Usage

log = logging.getLogger(__name__)

_ENVELOPE_KEYS = frozenset(
    {
        "v",
        "seq",
        "ts",
        "wall_ts",
        "run_id",
        "thread_id",
        "errand_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "node_attempt",
        "type",
    }
)


class UnknownTraceType(ValueError):
    pass


def trace_id_for(run_id: str) -> str:
    """Langfuse's create_trace_id(seed=run_id), reproduced without importing langfuse."""
    return hashlib.sha256(run_id.encode("utf-8")).digest()[:16].hex()


def format_ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("trace timestamps must be timezone-aware")
    return moment.isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class _Frame:
    tracer_id: int
    span_id: str
    node_attempt: int


# The innermost open span, per asyncio task. Frames carry their tracer's id so two
# runs in one task (tests do this) never adopt each other's spans.
_current: ContextVar[_Frame | None] = ContextVar("errand_trace_span", default=None)


@dataclass
class _Opening:
    tracer_id: int
    open_id: str
    record_type: str
    sinks: list[LiveSink]
    bound: bool = False


# The innermost Tracer.opening() of this task, waiting for its record.
_opening: ContextVar[_Opening | None] = ContextVar("errand_trace_opening", default=None)


@dataclass
class Span:
    span_id: str
    end_fields: dict[str, Any]


class Tracer:
    def __init__(
        self,
        *,
        run_id: str,
        thread_id: str,
        sinks: Sequence[Sink],
        now: Callable[[], datetime] | None = None,
        wall_now: Callable[[], datetime] = wall_now,
        perf: Callable[[], float] = time.perf_counter,
        errand_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.errand_id = errand_id
        self.trace_id = trace_id_for(run_id)
        self._sinks = list(sinks)
        self._now = now if now is not None else SystemClock().now
        self._wall_now = wall_now
        self._perf = perf
        self._seq = 0
        self._spans = 0
        self._node_attempts: Counter[str] = Counter()
        self._openings = 0
        self._run_span: str | None = None
        self._run_started: float | None = None
        self._model_calls = 0
        self._reader_calls = 0
        self._tool_calls = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._cost = Decimal(0)

    @property
    def perf(self) -> Callable[[], float]:
        """The monotonic clock latencies are measured with."""
        return self._perf

    # ------------------------------------------------------------------ core

    def _peek_span(self) -> str:
        # Span ids are only handed out once a record using them passes validation, so
        # a rejected record never leaves a hole in s_1, s_2, ...
        return f"s_{self._spans + 1}"

    def _frame(self) -> _Frame | None:
        frame = _current.get()
        return frame if frame is not None and frame.tracer_id == id(self) else None

    def emit(
        self,
        record_type: str,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Emit one record and return it exactly as the sinks received it."""
        return self._emit(
            record_type,
            span_id=self._peek_span() if span_id is None else span_id,
            parent_span_id=parent_span_id,
            new_span=span_id is None,
            fields=fields,
        )

    def _emit(
        self,
        record_type: str,
        *,
        span_id: str,
        parent_span_id: str | None,
        new_span: bool,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build and validate with candidate seq and span; commit them only if valid.

        `new_span` says `span_id` is the next unissued id, to be committed with the record.
        """
        if record_type not in RECORD_TYPES:
            raise UnknownTraceType(f"unknown trace record type {record_type!r}")
        clash = _ENVELOPE_KEYS & fields.keys()
        if clash:
            raise ValueError(f"envelope fields are assigned by the Tracer: {sorted(clash)}")
        frame = self._frame()
        payload = sanitize(fields)
        raw = {
            "seq": self._seq + 1,
            "ts": format_ts(self._now()),
            "wall_ts": format_ts(self._wall_now()),
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "errand_id": self.errand_id,
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id
            if parent_span_id is not None
            else (frame.span_id if frame else self._run_span),
            "node_attempt": frame.node_attempt if frame else 1,
            "type": record_type,
            **payload,
        }
        if raw["parent_span_id"] == raw["span_id"]:
            raw["parent_span_id"] = None
        model = RECORD_ADAPTER.validate_python(raw)
        record, line = encode_record(model)
        # Committed before fan-out on purpose: if the JSONL write itself fails, a line
        # may be half on disk, and reusing its seq would be worse than a gap.
        self._seq += 1
        if new_span:
            self._spans += 1
        self._bind_opening(record)
        self._fan_out(record, line)
        return record

    def _fan_out(self, record: Mapping[str, Any], line: str) -> None:
        for i, sink in enumerate(self._sinks):
            if i == 0:
                sink.write(record, line)
                continue
            try:
                sink.write(record, line)
            except Exception:
                log.exception("trace sink %s failed; record %s skipped there", sink, record["seq"])

    def _bind_opening(self, record: Mapping[str, Any]) -> None:
        """The record an open call was waiting for: tell its sinks which span it is, so
        they finish the observation they started instead of making a new one."""
        opening = _opening.get()
        if (
            opening is None
            or opening.bound
            or opening.tracer_id != id(self)
            or opening.record_type != record["type"]
        ):
            return
        opening.bound = True
        for sink in opening.sinks:
            try:
                sink.bind(opening.open_id, str(record["span_id"]))
            except Exception:
                log.exception("trace sink %s failed to bind %s", sink, opening.open_id)

    @contextmanager
    def opening(self, record_type: str, name: str) -> Iterator[None]:
        """Bracket a call whose `record_type` record is emitted inside this block.

        Writes nothing to the JSONL. Live sinks get `begin` on entry, `bind` when the
        first matching record of this tracer is emitted inside the block, and `abandon`
        if the block ends without one (an exception, or a refusal before the call).
        """
        if record_type not in RECORD_TYPES:
            raise UnknownTraceType(f"unknown trace record type {record_type!r}")
        live: list[LiveSink] = [s for s in self._sinks[1:] if isinstance(s, LiveSink)]
        if not live:
            yield
            return
        self._openings += 1
        open_id = f"o_{self._openings}"
        frame = self._frame()
        parent = frame.span_id if frame else self._run_span
        started: list[LiveSink] = []
        for sink in live:
            try:
                sink.begin(open_id, self.run_id, parent, record_type, name)
                started.append(sink)
            except Exception:
                log.exception("trace sink %s failed to begin %s", sink, record_type)
        opening = _Opening(id(self), open_id, record_type, started)
        token = _opening.set(opening)
        error = "no record"
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            _opening.reset(token)
            if not opening.bound:
                for sink in started:
                    try:
                        sink.abandon(open_id, error)
                    except Exception:
                        log.exception("trace sink %s failed to abandon %s", sink, open_id)

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                log.exception("trace sink %s failed to close", sink)

    # ------------------------------------------------------------------ spans

    @contextmanager
    def node(self, name: str, **start_fields: Any) -> Iterator[Span]:
        """node.start ... node.end around a graph node, with its attempt number."""
        attempt = self._node_attempts[name] + 1
        span_id = self._peek_span()
        parent = self._frame()
        token = _current.set(_Frame(id(self), span_id, attempt))
        started = self._perf()
        span = Span(span_id, {})
        try:
            self._emit(
                "node.start",
                span_id=span_id,
                parent_span_id=parent.span_id if parent else self._run_span,
                new_span=True,
                fields={"node": name, **start_fields},
            )
            # Only a node whose start was recorded counts as an attempt.
            self._node_attempts[name] = attempt
            try:
                yield span
            except BaseException as exc:
                span.end_fields.setdefault("error", type(exc).__name__)
                raise
            finally:
                self.emit(
                    "node.end",
                    span_id=span_id,
                    parent_span_id=parent.span_id if parent else self._run_span,
                    node=name,
                    latency_ms=(self._perf() - started) * 1000,
                    **span.end_fields,
                )
        finally:
            _current.reset(token)

    # ------------------------------------------------------------------ run

    def start_run(self, *, trigger: str, **fields: Any) -> dict[str, Any]:
        if self._run_span is not None:
            raise RuntimeError("run already started")
        self._run_started = self._perf()
        span_id = self._peek_span()
        record = self._emit(
            "run.start",
            span_id=span_id,
            parent_span_id=None,
            new_span=True,
            fields={"trigger": trigger, **fields},
        )
        self._run_span = span_id
        return record

    def end_run(self, *, outcome: str, **fields: Any) -> dict[str, Any]:
        """run.end, with totals summed from this run's model.call and tool.call records."""
        started = self._run_started if self._run_started is not None else self._perf()
        totals = RunTotals(
            model_calls=self._model_calls,
            tool_calls=self._tool_calls,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            cost_usd=float(self._cost),
            latency_ms=(self._perf() - started) * 1000,
            reader_calls=self._reader_calls,
        )
        return self.emit(
            "run.end",
            span_id=self._run_span,
            parent_span_id=None,
            outcome=outcome,
            totals=totals.model_dump(),
            **fields,
        )

    # ------------------------------------------------------------------ typed helpers

    def model_call(
        self,
        *,
        model: str,
        usage: Usage,
        cost: Decimal,
        started: float,
        first_token: float | None,
        ended: float,
        **fields: Any,
    ) -> dict[str, Any]:
        """model.call with latency from perf-counter readings.

        The caller prices the call (errand.llm.pricing) and passes the exact Decimal;
        the tracer sits below the model gateway and never imports it.
        """
        record = self.emit(
            "model.call",
            model=model,
            usage=usage.model_dump(),
            cost_usd=float(cost),
            latency_ms=(ended - started) * 1000,
            ttft_ms=None if first_token is None else (first_token - started) * 1000,
            **fields,
        )
        # Totals are summed from what was actually written, so run.end matches the
        # records line for line (INV-14).
        self._model_calls += 1
        self._tokens_in += usage.input_total
        self._tokens_out += usage.output
        self._cost += Decimal(str(record["cost_usd"]))
        return record

    def reader_call(
        self, *, called: bool, usage: Usage | None, cost: Decimal | None, **fields: Any
    ) -> dict[str, Any]:
        """reader.call. A read that went to the model (`called`, whether or not it then
        failed) counts as a reader call, never as a model call; whatever usage and cost
        it reported go into the run totals."""
        record = self.emit(
            "reader.call",
            usage=None if usage is None else usage.model_dump(),
            cost_usd=None if cost is None else float(cost),
            **fields,
        )
        if called:
            self._reader_calls += 1
        if usage is not None:
            self._tokens_in += usage.input_total
            self._tokens_out += usage.output
        if record["cost_usd"] is not None:
            self._cost += Decimal(str(record["cost_usd"]))
        return record

    def tool_call(self, **fields: Any) -> dict[str, Any]:
        record = self.emit("tool.call", **fields)
        self._tool_calls += 1
        return record
