"""The Langfuse sink through the real API: Langfuse shows exactly what the JSONL says.

An S03-shaped conversation (two questions, a shortlist, a reply: four runs on one
thread) runs through the eval harness with a Langfuse sink whose spans land in an
in-memory exporter instead of a server. Every check compares the exported spans with
the run's own JSONL trace (architecture 16.4, ADR 0008).
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from errand.config import LimitSettings, TraceSettings
from errand.trace.sanitize import PHONE_MASK
from errand.trace.sinks import make_sink
from errand.trace.sinks.langfuse import TRACE_NAME, LangfuseSink
from tests.trace.langfuse_support import (
    PHONES,
    api,
    decoded,
    expected_outcomes,
    fresh_keys,
    jsonl_runs,
    review_digests,
    s03,
    stable,
    string_leaves,
    world_review_texts,
)

ON = TraceSettings(langfuse="auto")
TYPE_OF = {
    "run.start": "agent",
    "node.start": "chain",
    "model.call": "generation",
    "reader.call": "generation",
    "tool.call": "tool",
}
# Below the first call's request, above nothing: every planner call is refused.
REFUSING_BUDGET = 200
# Attributes Langfuse adds itself (SDK bookkeeping and our fixed trace name), not
# record content.
SDK_ATTRS = ("langfuse.internal.", "langfuse.environment", "langfuse.release", "langfuse.version")


def expected_type(record_type: str) -> str:
    if record_type in TYPE_OF:
        return TYPE_OF[record_type]
    if record_type.startswith(("guard.", "approval.")):
        return "guardrail"
    return "event"


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def meta(span: ReadableSpan, key: str) -> Any:
    return decoded(attrs(span).get(f"langfuse.observation.metadata.{key}"))


def otel_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id


def trace_hex(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.trace_id, "032x")


def duration_ms(span: ReadableSpan) -> float:
    assert span.start_time is not None and span.end_time is not None
    return (span.end_time - span.start_time) / 1e6


@pytest.fixture
def exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the conversation once; hand back (JSONL by run, finished spans, thread id)."""

    async def run() -> tuple[dict[str, list[dict[str, Any]]], list[ReadableSpan], str, list[str]]:
        exporter = InMemorySpanExporter()
        keys = fresh_keys()
        async with api(
            tmp_path, monkeypatch, lambda: make_sink(ON, keys, span_exporter=exporter), slow=True
        ) as h:
            outcomes = await s03(h)
            thread = h.conversation_id
            health = (await h.client.get("/api/health")).json()
            assert health["langfuse"] == {
                "state": "on",
                "host": "http://127.0.0.1:9",
                "export_failures": 0,
            }
        # The lifespan exit has flushed and shut the client down by now.
        return jsonl_runs(tmp_path), list(exporter.get_finished_spans()), thread, outcomes

    return run


async def test_langfuse_mirrors_the_jsonl(exported: Any, tmp_path: Path) -> None:
    runs, spans, thread, outcomes = await exported()
    assert outcomes == expected_outcomes()
    assert len(runs) == 4
    assert_mirrors(runs, spans, thread)
    for records in runs.values():
        assert any(r["type"] == "model.call" for r in records)

    # A generation of the S03 shape lasts at least the slow model's 20 ms.
    shortlist = runs[sorted(runs)[2]]
    assert any(r["type"] == "tool.call" and r["tool"] == "get_provider" for r in shortlist)
    longest = max(
        duration_ms(s) for s in spans if attrs(s)["langfuse.observation.type"] == "generation"
    )
    assert longest >= 20


def assert_mirrors(
    runs: dict[str, list[dict[str, Any]]], spans: list[ReadableSpan], thread: str
) -> None:
    """Langfuse holds exactly the JSONL's runs: same ids, parents, types, costs, order."""
    # One trace per run, under the record's own trace id.
    by_trace: dict[str, list[ReadableSpan]] = defaultdict(list)
    for span in spans:
        by_trace[trace_hex(span)].append(span)
    run_trace = {records[0]["trace_id"]: run_id for run_id, records in runs.items()}
    assert set(by_trace) == set(run_trace)
    for run_id, records in runs.items():
        assert {r["trace_id"] for r in records} == {records[0]["trace_id"]}, run_id

    for trace_id, trace_spans in by_trace.items():
        records = runs[run_trace[trace_id]]
        record_of = {r["span_id"]: r for r in records}
        opening: dict[str, dict[str, Any]] = {}  # span_id -> the record that opened it
        for r in records:
            opening.setdefault(r["span_id"], r)

        # Session and trace name on every span, children and events included.
        for span in trace_spans:
            a = attrs(span)
            assert a.get("session.id") == thread, span.name
            assert a.get("langfuse.trace.name") == TRACE_NAME, span.name

        # Exactly one span per JSONL span id, of the mapped type, with its seq.
        span_of = {}
        for span in trace_spans:
            span_id = meta(span, "span_id")
            assert span_id not in span_of, f"two spans for {span_id}"
            span_of[span_id] = span
        assert set(span_of) == set(opening)
        for span_id, span in span_of.items():
            first = opening[span_id]
            assert attrs(span)["langfuse.observation.type"] == expected_type(first["type"])
            assert meta(span, "seq") == first["seq"]
            assert attrs(span).get("langfuse.observation.level") is None, span.name

        # The parent structure mirrors parent_span_id.
        roots = [s for s in trace_spans if attrs(s)["langfuse.observation.type"] == "agent"]
        assert len(roots) == 1
        for span_id, span in span_of.items():
            parent = record_of[span_id]["parent_span_id"]
            if parent is None:
                assert span is roots[0]
            else:
                assert span.parent is not None
                assert span.parent.span_id == otel_id(span_of[parent]), span_id

        # Our cost, and only ours, on the generations; it adds up to the run's total.
        end = next(r for r in records if r["type"] == "run.end")
        generations = [
            s for s in trace_spans if attrs(s)["langfuse.observation.type"] == "generation"
        ]
        costs = [decoded(attrs(g)["langfuse.observation.cost_details"]) for g in generations]
        assert all(set(c) == {"total"} for c in costs)
        assert sum(c["total"] for c in costs) == pytest.approx(end["totals"]["cost_usd"], abs=1e-9)
        model_calls = [r for r in records if r["type"] == "model.call"]
        assert len(generations) == len(model_calls) == end["totals"]["model_calls"]
        for g in generations:
            usage = record_of[meta(g, "span_id")]["usage"]
            details = decoded(attrs(g)["langfuse.observation.usage_details"])
            assert details["input"] == usage["input_uncached"]
            assert details["input_cache_read"] == usage["cache_read"]
            assert details["input_cache_creation"] == (
                usage["cache_write_5m"] + usage["cache_write_1h"]
            )
            assert details["output"] == usage["output"]

        # Generations and tools ran live: their spans last as long as the call did.
        for span_id, span in span_of.items():
            record = record_of[span_id]
            if record["type"] not in ("model.call", "tool.call"):
                continue
            took = duration_ms(span)
            assert took > 0, span_id
            assert record["latency_ms"] - 0.05 <= took <= record["latency_ms"] + 100, (
                f"{record['type']} {span_id}: span {took:.3f} ms, record "
                f"{record['latency_ms']:.3f} ms"
            )

        # Each generation starts after the context.built it was sent with, under the
        # same parent: the call opens only once the budget checks have passed.
        for record in records:
            if record["type"] != "model.call":
                continue
            built = [
                r
                for r in records
                if r["type"] == "context.built"
                and r["parent_span_id"] == record["parent_span_id"]
                and r["seq"] < record["seq"]
            ]
            assert built, f"model.call {record['span_id']} has no context.built before it"
            assert built[-1]["manifest"]["sent"] is True
            context = span_of[built[-1]["span_id"]]
            generation = span_of[record["span_id"]]
            assert context.start_time is not None and generation.start_time is not None
            assert generation.start_time >= context.start_time, (
                f"generation {record['span_id']} starts before context.built {built[-1]['span_id']}"
            )


# Limits that refuse a model call inside LLMGateway.send(), before the model is called:
# the call cap (refused before context.built) and the context budget (refused after a
# context.built that says sent: false).
REFUSALS = {
    "model_call_cap": LimitSettings(model_calls_per_run=1),
    "context_budget": LimitSettings(context_budget_tokens=REFUSING_BUDGET),
}


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
async def test_a_refused_model_call_exports_no_generation(
    refusal: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = REFUSALS[refusal]
    exporter = InMemorySpanExporter()
    keys = fresh_keys()
    async with api(
        tmp_path, monkeypatch, lambda: make_sink(ON, keys, span_exporter=exporter), limits=limits
    ) as h:
        await s03(h)
        thread = h.conversation_id
    runs = jsonl_runs(tmp_path)
    spans = list(exporter.get_finished_spans())

    # The JSONL shows the refusal, in every run. The cap: one model call, then the
    # second is refused before its context.built. The budget: send() writes a
    # context.built that says it was not sent, and no run calls the model at all.
    want = "LIMIT_REACHED" if refusal == "model_call_cap" else "CONTEXT_TOO_LARGE"
    for run_id, records in runs.items():
        end = records[-1]
        assert end["type"] == "run.end"
        assert end["outcome"] == want, run_id
        built = [r for r in records if r["type"] == "context.built"]
        calls = [r for r in records if r["type"] == "model.call"]
        assert len([r for r in built if r["manifest"]["sent"]]) == len(calls)
        if refusal == "model_call_cap":
            assert len(calls) == end["totals"]["model_calls"] == 1, run_id
        else:
            assert not calls, run_id
            (unsent,) = built
            manifest = unsent["manifest"]
            assert manifest["sent"] is False and "over_cap" not in manifest
            assert manifest["budget_tokens"] == REFUSING_BUDGET < manifest["est_request_tokens"]

    # No generation and no ERROR observation for the refused call: Langfuse still holds
    # exactly what the JSONL says.
    generations = [s for s in spans if attrs(s)["langfuse.observation.type"] == "generation"]
    model_calls = [r for rs in runs.values() for r in rs if r["type"] == "model.call"]
    assert len(generations) == len(model_calls)
    assert not [s for s in spans if attrs(s).get("langfuse.observation.level") == "ERROR"]
    assert_mirrors(runs, spans, thread)


async def test_nothing_reaches_langfuse_that_the_jsonl_does_not_hold(
    exported: Any, tmp_path: Path
) -> None:
    runs, spans, _, _ = await exported()
    jsonl_strings = {s for records in runs.values() for s in string_leaves(records)}
    for span in spans:
        assert span.name in jsonl_strings, span.name
        for key, value in attrs(span).items():
            if key.startswith(SDK_ATTRS) or key in (
                "langfuse.trace.name",
                "langfuse.observation.type",
                "langfuse.observation.level",
            ):
                continue
            missing = [s for s in string_leaves(decoded(value)) if s not in jsonl_strings]
            assert not missing, f"{span.name}.{key}: {missing[:3]}"

    # The proof: the seeded phone and the raw review text are in the world, were read
    # by the planner's tools, and still never reached Langfuse. First, that they were
    # read: the masked phone and the review digests are in the JSONL. Without that the
    # absence checks below would pass vacuously.
    dump = repr([(s.name, attrs(s)) for s in spans])
    reviews = [t for t in world_review_texts(tmp_path) if len(t) >= 20]
    assert reviews
    assert any(f"WhatsApp {PHONE_MASK}" in s for s in jsonl_strings)
    digests = [d for records in runs.values() for d in review_digests(records)]
    assert digests
    world_hashes = {hashlib.sha256(t.encode("utf-8")).hexdigest() for t in reviews}
    assert world_hashes & {d["sha256"] for d in digests}
    for secret in (*PHONES, *reviews):
        assert secret not in dump, secret
        assert all(secret not in s for s in jsonl_strings), secret


class Raising:
    """A Langfuse client where every call fails."""

    def __init__(self, **kwargs: Any) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise RuntimeError(f"langfuse {name} is broken")

        return fail


async def test_a_failing_langfuse_costs_one_log_line_per_run_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sinks: list[Any] = []

    def broken() -> Any:
        sink = make_sink(ON, fresh_keys(), client_factory=Raising)
        sinks.append(sink)
        return sink

    caplog.set_level(logging.INFO)
    async with api(tmp_path / "on", monkeypatch, broken) as h:
        assert await s03(h) == expected_outcomes()
    async with api(tmp_path / "off", monkeypatch) as h:
        await s03(h)

    assert isinstance(sinks[0], LangfuseSink)
    assert sinks[0].client.calls > 0
    broken_runs = jsonl_runs(tmp_path / "on")
    # Every run still ends with run.end, and the JSONL is the same as with Langfuse off.
    assert all(records[-1]["type"] == "run.end" for records in broken_runs.values())
    assert stable(broken_runs) == stable(jsonl_runs(tmp_path / "off"))

    failures = [
        r
        for r in caplog.records
        if r.name == "errand.trace.sinks.langfuse" and "Langfuse sink failed" in r.getMessage()
    ]
    assert len(failures) == len(broken_runs) == 4
    assert Counter(r.getMessage().split(" in run ")[1].split(";")[0] for r in failures) == {
        run_id: 1 for run_id in broken_runs
    }
    # The Tracer never saw a sink error: the view swallowed them all.
    assert not [r for r in caplog.records if r.name == "errand.trace.tracer"]
