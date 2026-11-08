"""LangfuseSink: the optional dashboard (ADR 0008, architecture 16.4).

The JSONL trace is the record; this is a view of the same records, already sanitized
once by the Tracer. It reads only the record it is handed and adds nothing, so there is
no second sanitizer and the SDK's `mask` is not used.

One `LangfuseSink` per process, built by `make_sink()` in the API lifespan, and one
`_RunView` per run from `for_run()`. The view is what goes into the run's Tracer. Its
`close()` ends whatever that run left open and forgets the run; it never touches the
client, because `Tracer.close()` runs at the end of every run and a client shut down
there would export nothing for the next run. Only `shutdown()`, from the lifespan exit,
flushes and stops the client.

Record to observation (16.4):
- `run.start` opens the run's root `agent` observation, in the trace whose id is the
  record's own `trace_id`; `run.end` closes it.
- `node.start` / `node.end`: `chain`.
- `model.call`, `reader.call`: `generation`, with our usage split and our cost.
- `tool.call`: `tool`.
- `guard.*`, `approval.*`: `guardrail`.
- anything else: an event on its parent.
Parents follow `parent_span_id`. Every observation carries our `span_id` and `seq` in
its metadata, so a Langfuse row leads straight to its JSONL line.

Calls are live: `Tracer.opening()` lets this sink start the generation or tool before
the call (`begin`) and finish that same observation when the record arrives (`bind`,
then `write`). v5 cannot set a start time (spike F3), and passing our recorded
timestamps as end times gives negative durations, so no time is ever passed.

Every observation start, children and events included, is wrapped in
`propagate_attributes`: in v5 that is the only way the session id and trace name reach
child spans (spike F6).

Failure: every view method catches its own errors and logs the first one per run, so a
broken Langfuse costs one log line per run and the run carries on. Export runs on the
SDK's background thread and is never flushed per request or per run (`flush()` blocks
the caller for the whole timeout when the server is down, spike F8). Failed exports are
counted for /api/health.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse._version import __version__ as langfuse_version
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from errand.config import LangfuseKeys

log = logging.getLogger(__name__)

SpanExporterLike = SpanExporter
# Builds the client. Langfuse itself in production; tests pass a failing one (A4).
ClientFactory = Callable[..., Any]

TRACE_NAME = "errand_run"
SERVICE_NAME = "errand-api"
ENVIRONMENT = "default"
RELEASE = "errand"
# Small batches every second: the dashboard trails the JSONL by seconds, not minutes.
FLUSH_AT = 64
FLUSH_INTERVAL_S = 1.0

_ENVELOPE = frozenset(
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
_CALL_TYPES = {"model.call": "generation", "reader.call": "generation", "tool.call": "tool"}
_OTLP_LOGGER = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def _payload(record: Mapping[str, Any], *drop: str) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in _ENVELOPE and k not in drop}


def _meta(record: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    meta = {"span_id": record["span_id"], "seq": record["seq"]}
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def usage_details(usage: Mapping[str, Any]) -> dict[str, int]:
    """Our split, never LangChain's `input_tokens`, which already includes cache reads
    and writes and would be counted twice. `reasoning` is part of `output`, so it is
    left out of the sum."""
    cache_write = int(usage.get("cache_write_5m", 0)) + int(usage.get("cache_write_1h", 0))
    details = {
        "input": int(usage.get("input_uncached", 0)),
        "input_cache_read": int(usage.get("cache_read", 0)),
        "input_cache_creation": cache_write,
        "output": int(usage.get("output", 0)),
    }
    details["total"] = sum(details.values())
    return details


class _QuietOtlp(logging.Filter):
    """The OTLP exporter logs every failed batch; with the server down that is a line a
    second. The first failure and the recovery are logged here instead, and the count is
    in /api/health."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.CRITICAL


class _CountingExporter(SpanExporter):
    """Counts failed export batches, and stops trying once shutdown's deadline passes."""

    def __init__(self, inner: SpanExporter, host: str) -> None:
        self._inner = inner
        self._host = host
        self._lock = threading.Lock()
        self.failures = 0
        self._failing = False
        self._deadline: float | None = None

    def stop_at(self, deadline: float) -> None:
        self._deadline = deadline

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._record(ok=False, why="shutdown deadline passed, batch dropped")
            return SpanExportResult.FAILURE
        try:
            result = self._inner.export(spans)
        except Exception as exc:
            self._record(ok=False, why=type(exc).__name__)
            return SpanExportResult.FAILURE
        self._record(ok=result is SpanExportResult.SUCCESS, why="export failed")
        return result

    def _record(self, *, ok: bool, why: str) -> None:
        with self._lock:
            if ok:
                if self._failing:
                    log.info("Langfuse export to %s recovered", self._host)
                self._failing = False
                return
            self.failures += 1
            first = not self._failing
            self._failing = True
        if first:
            log.warning(
                "Langfuse export to %s failed (%s); traces stay in JSONL, failed batches "
                "are counted in /api/health",
                self._host,
                why,
            )

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _otlp_exporter(public_key: str, secret_key: str, host: str, timeout: int) -> SpanExporter:
    """The exporter Langfuse would build for itself (5.2.0, LangfuseSpanProcessor), built
    here so failed batches can be counted."""
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {auth}",
            "x-langfuse-sdk-name": "python",
            "x-langfuse-sdk-version": langfuse_version,
            "x-langfuse-public-key": public_key,
            "x-langfuse-ingestion-version": "4",
        },
        timeout=timeout,
        compression=Compression.Gzip,
    )


class LangfuseSink:
    """The process-level Langfuse sink. See the module docstring."""

    def __init__(
        self,
        keys: LangfuseKeys,
        *,
        span_exporter: SpanExporter | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if keys.public_key is None or keys.secret_key is None:
            raise ValueError("LangfuseSink needs both keys; make_sink() checks this")
        public_key = keys.public_key.get_secret_value()
        secret_key = keys.secret_key.get_secret_value()
        self.host = keys.host.rstrip("/")
        self.timeout = keys.timeout
        self._public_key = public_key
        inner = span_exporter or _otlp_exporter(public_key, secret_key, self.host, self.timeout)
        self._exporter = _CountingExporter(inner, self.host)
        self._quiet: _QuietOtlp | None = None
        if span_exporter is None:
            self._quiet = _QuietOtlp()
            logging.getLogger(_OTLP_LOGGER).addFilter(self._quiet)
        # A private provider: the global one belongs to whoever else uses OTel in this
        # process. Its resource, sampler and exit behaviour are given, not read from
        # OTEL_* variables, and its atexit flush is off because shutdown() owns that.
        self._provider = TracerProvider(
            resource=Resource({"service.name": SERVICE_NAME}),
            sampler=ALWAYS_ON,
            shutdown_on_exit=False,
        )
        factory: ClientFactory = client_factory or Langfuse
        # Every setting is passed explicitly so the SDK falls back to no LANGFUSE_*
        # variable of its own.
        self.client = factory(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self.host,
            timeout=self.timeout,
            debug=False,
            tracing_enabled=True,
            flush_at=FLUSH_AT,
            flush_interval=FLUSH_INTERVAL_S,
            environment=ENVIRONMENT,
            release=RELEASE,
            media_upload_thread_count=1,
            sample_rate=1.0,
            tracer_provider=self._provider,
            span_exporter=self._exporter,
        )
        self._lock = threading.Lock()
        self.sink_errors = 0
        self._closed = False

    def for_run(self, run_id: str, thread_id: str) -> _RunView:
        return _RunView(self, run_id, thread_id)

    def status(self) -> dict[str, Any]:
        return {"state": "on", "host": self.host, "export_failures": self._exporter.failures}

    def propagate(self, thread_id: str, run_id: str) -> AbstractContextManager[Any]:
        return propagate_attributes(
            session_id=thread_id, trace_name=TRACE_NAME, metadata={"run_id": run_id}
        )

    def count_error(self) -> None:
        with self._lock:
            self.sink_errors += 1

    def shutdown(self) -> None:
        """Flush what is queued and stop the client, within `timeout` + 0.5 s.

        With the server down every batch export takes up to `timeout`, so a long queue
        could hold the lifespan exit for many timeouts. Batches that would start after
        the deadline are dropped (and counted), and the whole stop runs on a thread that
        is waited for no longer than the bound.
        """
        if self._closed:
            return
        self._closed = True
        started = time.monotonic()
        self._exporter.stop_at(started + self.timeout)
        done = threading.Event()

        def stop() -> None:
            try:
                for step in (self.client.shutdown, self._provider.shutdown):
                    try:
                        step()
                    except Exception:
                        log.exception("Langfuse shutdown step %s failed", step.__qualname__)
            finally:
                done.set()

        threading.Thread(target=stop, name="langfuse-shutdown", daemon=True).start()
        if not done.wait(self.timeout + 0.5):
            log.warning(
                "Langfuse shutdown did not finish in %.1fs; unexported spans are dropped",
                time.monotonic() - started,
            )
        self._forget_client()
        if self._quiet is not None:
            logging.getLogger(_OTLP_LOGGER).removeFilter(self._quiet)

    def _forget_client(self) -> None:
        # Langfuse 5.2.0 keeps one resource manager per public key for the life of the
        # process, and shutdown() leaves it registered: a new client with the same key
        # would get this dead one, exporter and all, and export nothing. Only matters
        # when one process starts the API twice (tests), but then it matters silently.
        try:
            from langfuse._client.resource_manager import LangfuseResourceManager

            with LangfuseResourceManager._lock:
                LangfuseResourceManager._instances.pop(self._public_key, None)
        except Exception:
            log.debug("could not unregister the Langfuse resource manager", exc_info=True)


class _RunView:
    """One run's records as observations. Never raises; see the module docstring."""

    def __init__(self, owner: LangfuseSink, run_id: str, thread_id: str) -> None:
        self._owner = owner
        self._client = owner.client
        self.run_id = run_id
        self.thread_id = thread_id
        self._lock = threading.Lock()
        self._root: Any = None
        self._trace_id: str | None = None
        self._open: dict[str, Any] = {}  # span_id -> started, not yet ended (run, nodes)
        self._begun: dict[str, Any] = {}  # open_id -> a call started by begin(), not bound
        self._bound: dict[str, Any] = {}  # span_id -> a begun call, waiting for its record
        self._logged = False

    # ---------------------------------------------------------------- Sink

    def write(self, record: Mapping[str, Any], line: str) -> None:
        with self._lock, self._guard("writing " + str(record.get("type"))):
            self._write(record)

    def close(self) -> None:
        """End whatever this run left open (a run cut short) and forget the run. The
        client is the process's, so it is not touched here."""
        with self._lock:
            with self._guard("closing"):
                leftovers = [*self._begun.values(), *self._bound.values()]
                leftovers += [obs for obs in self._open.values() if obs is not self._root]
                if self._root is not None:
                    leftovers.append(self._root)
                for obs in leftovers:
                    obs.update(level="WARNING", status_message="still open when the run closed")
                    obs.end()
            self._begun.clear()
            self._bound.clear()
            self._open.clear()
            self._root = None

    # ---------------------------------------------------------------- LiveSink

    def begin(
        self, open_id: str, run_id: str, parent_span_id: str | None, kind: str, name: str
    ) -> None:
        with self._lock, self._guard("starting " + kind):
            as_type = _CALL_TYPES.get(kind, "span")
            extra = {"model": name} if as_type == "generation" else {}
            self._begun[open_id] = self._start(as_type, name, parent_span_id, **extra)

    def bind(self, open_id: str, span_id: str) -> None:
        with self._lock, self._guard("binding a call"):
            obs = self._begun.pop(open_id, None)
            if obs is not None:
                self._bound[span_id] = obs

    def abandon(self, open_id: str, error: str) -> None:
        with self._lock, self._guard("abandoning a call"):
            obs = self._begun.pop(open_id, None)
            if obs is not None:
                obs.update(level="ERROR", status_message=error)
                obs.end()

    # ---------------------------------------------------------------- mapping

    def _write(self, record: Mapping[str, Any]) -> None:
        kind = str(record["type"])
        if kind == "run.start":
            self._trace_id = str(record["trace_id"])
            self._root = self._start(
                "agent",
                str(record["trigger"]),
                None,
                input=_payload(record),
                metadata=_meta(record, run_id=self.run_id),
            )
            self._open[str(record["span_id"])] = self._root
        elif kind == "run.end":
            root = self._open.pop(str(record["span_id"]), None) or self._root
            if root is not None:
                root.update(output=_payload(record), metadata={"end_seq": record["seq"]})
                root.end()
            self._root = None
        elif kind == "node.start":
            self._open[str(record["span_id"])] = self._start(
                "chain",
                str(record["node"]),
                record.get("parent_span_id"),
                input=_payload(record),
                metadata=_meta(record),
            )
        elif kind == "node.end":
            obs = self._open.pop(str(record["span_id"]), None)
            if obs is not None:
                error = record.get("error")
                obs.update(
                    output=_payload(record),
                    metadata={"end_seq": record["seq"]},
                    **({"level": "ERROR", "status_message": str(error)} if error else {}),
                )
                obs.end()
        elif kind in _CALL_TYPES:
            self._finish_call(record)
        elif kind.startswith(("guard.", "approval.")):
            obs = self._start(
                "guardrail",
                kind,
                record.get("parent_span_id"),
                input=_payload(record),
                metadata=_meta(record),
                **({"level": "WARNING"} if kind.startswith("guard.") else {}),
            )
            obs.end()
        else:
            extra: dict[str, Any] = {}
            if kind == "error":
                extra = {"level": "ERROR", "status_message": str(record.get("exception_type"))}
            self._event(record, **extra)

    def _finish_call(self, record: Mapping[str, Any]) -> None:
        kind = str(record["type"])
        as_type = _CALL_TYPES[kind]
        span_id = str(record["span_id"])
        update: dict[str, Any] = {}
        if kind == "model.call":
            name = str(record["model"])
            update = {
                "model": name,
                "input": record.get("request"),
                "output": record.get("response"),
                "metadata": _meta(
                    record,
                    request_sha256=record.get("request_sha256"),
                    stop_reason=record.get("stop_reason"),
                ),
            }
        elif kind == "reader.call":
            name = str(record.get("model") or "reader")
            update = {
                "input": _payload(record, "labels", "usage", "cost_usd"),
                "output": record.get("labels"),
                "metadata": _meta(record),
            }
            if record.get("model"):
                update["model"] = record["model"]
        else:
            name = str(record["tool"])
            error = record.get("error")
            update = {
                "input": record.get("args"),
                "output": {"error": error} if error else record.get("result"),
                "metadata": _meta(
                    record,
                    caller=record.get("caller"),
                    attempt=record.get("attempt"),
                    idempotency_key=record.get("idempotency_key"),
                    world_seq=record.get("world_seq"),
                ),
            }
            if error:
                update.update(level="ERROR", status_message=str(error))
        if as_type == "generation":
            if record.get("usage") is not None:
                update["usage_details"] = usage_details(record["usage"])
            # Our cost, always: Langfuse must never price the call from its own table.
            if record.get("cost_usd") is not None:
                update["cost_details"] = {"total": float(record["cost_usd"])}
        obs = self._bound.pop(span_id, None)
        if obs is None:
            # Emitted outside Tracer.opening(): still shown, but with no duration.
            extra = {"model": update["model"]} if "model" in update else {}
            obs = self._start(as_type, name, record.get("parent_span_id"), **extra)
        obs.update(**update)
        obs.end()

    def _parent(self, parent_span_id: Any) -> Any:
        if parent_span_id is not None and str(parent_span_id) in self._open:
            return self._open[str(parent_span_id)]
        return self._root

    def _start(self, as_type: str, name: str, parent_span_id: Any, **fields: Any) -> Any:
        parent = self._parent(parent_span_id)
        with self._owner.propagate(self.thread_id, self.run_id):
            if parent is not None:
                return parent.start_observation(name=name, as_type=as_type, **fields)
            if self._trace_id is None:
                raise RuntimeError("no run.start seen and no trace id to start from")
            return self._client.start_observation(
                trace_context={"trace_id": self._trace_id}, name=name, as_type=as_type, **fields
            )

    def _event(self, record: Mapping[str, Any], **fields: Any) -> None:
        parent = self._parent(record.get("parent_span_id"))
        name = str(record["type"])
        payload = _payload(record)
        with self._owner.propagate(self.thread_id, self.run_id):
            if parent is not None:
                parent.create_event(name=name, input=payload, metadata=_meta(record), **fields)
            else:
                self._client.create_event(
                    trace_context={"trace_id": str(record["trace_id"])},
                    name=name,
                    input=payload,
                    metadata=_meta(record),
                    **fields,
                )

    @contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._owner.count_error()
            if not self._logged:
                self._logged = True
                log.warning(
                    "Langfuse sink failed while %s in run %s; the JSONL trace is "
                    "unaffected, and further Langfuse failures in this run are not logged",
                    what,
                    self.run_id,
                    exc_info=True,
                )
