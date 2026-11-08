"""Helpers for the Langfuse sink tests: the real API with a dashboard sink injected.

The harness is the eval runner's, so these runs go through the same lifespan, routes,
graph and scripted reference policy as `make evals`. The only change is the app's
`sink_factory`, swapped in where the runner builds its AppConfig.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import SecretStr

from errand.api.app import AppConfig
from errand.config import LangfuseKeys
from errand.llm.scripted import ScriptedChatModel
from errand.trace.reader import read_trace
from errand.trace.sinks.base import ProcessSink, Sink
from errand.world.scenario import ScenarioConfig
from evals import runner
from evals.policies import reference
from evals.runner import Harness

# Seeded into one provider's `about` text in world.sqlite: a Bengaluru landline and an
# Indian mobile, both shapes the sanitizer masks (BUG-M2-2).
PHONES = ("080 4123 4567", "+91 98450 12345")

# S03's shape: two questions, a shortlist, then a reply from it. Four runs, one thread.
S03_TURNS = (
    ("need a plumber", "ASKED_USER"),
    ("indiranagar", "ASKED_USER"),
    ("this week mornings, 1500", "PRESENTED_OPTIONS"),
    ("which one is cheapest?", "REPLIED"),
)

# Fields that differ between two otherwise identical runs: real time and real latency.
VOLATILE = frozenset({"wall_ts", "latency_ms", "ttft_ms"})


def s03_world() -> ScenarioConfig:
    return ScenarioConfig(id="S03_terse_four_turns", seed=3)


def fresh_keys(host: str = "http://127.0.0.1:9", timeout: int = 2) -> LangfuseKeys:
    """A public key no other test uses: Langfuse 5.2.0 shares one exporter per public key
    within a process (spike F2), so reusing one would send spans to another test."""
    token = uuid.uuid4().hex[:12]
    return LangfuseKeys(
        public_key=SecretStr(f"pk-test-{token}"),
        secret_key=SecretStr(f"sk-test-{token}"),
        host=host,
        timeout=timeout,
    )


class SlowModel(ScriptedChatModel):
    """The reference planner, 20 ms per call, so a call has a duration worth checking."""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        await asyncio.sleep(0.02)
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


def slow_factory() -> SlowModel:
    return SlowModel(script=reference.policy)


@asynccontextmanager
async def api(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    sink_factory: Callable[[], ProcessSink] | None = None,
    *,
    slow: bool = False,
) -> AsyncIterator[Harness]:
    """The eval harness (real app, lifespan included) with `sink_factory` swapped in.

    Without one, the app builds its sink from the environment, which the suite's
    conftest forces off.
    """
    with patched_sink(monkeypatch, sink_factory):
        h = Harness(root, s03_world(), reference.policy, slow_factory if slow else None)
        async with h:
            seed_phone(root)
            yield h


@contextmanager
def patched_sink(
    monkeypatch: pytest.MonkeyPatch, sink_factory: Callable[[], ProcessSink] | None
) -> Iterator[None]:
    """Swap `sink_factory` into the AppConfig the eval runner builds, for this block only."""
    if sink_factory is None:
        yield
        return
    real = runner.create_app
    factory = sink_factory

    def create_app(config: AppConfig | None = None) -> Any:
        assert config is not None
        return real(replace(config, sink_factory=factory))

    with monkeypatch.context() as patch:
        patch.setattr(runner, "create_app", create_app)
        yield


def seed_phone(root: Path) -> None:
    conn = sqlite3.connect(root / "world.sqlite")
    try:
        with conn:
            conn.execute(
                "UPDATE providers SET about = about || ? WHERE service = 'plumber'",
                (f" Call {PHONES[0]} or WhatsApp {PHONES[1]}.",),
            )
    finally:
        conn.close()


async def s03(h: Harness) -> list[str]:
    outcomes = []
    for text, _ in S03_TURNS:
        view = await h.say(text)
        outcomes.append(view.outcome)
    await h.manager.wait_idle()
    return [o or "" for o in outcomes]


def expected_outcomes() -> list[str]:
    return [outcome for _, outcome in S03_TURNS]


def jsonl_runs(root: Path) -> dict[str, list[dict[str, Any]]]:
    """run_id -> records, for every trace file under root."""
    return {
        path.stem: read_trace(path).records for path in sorted((root / "traces").glob("*/*.jsonl"))
    }


def stable(record: Any) -> Any:
    """A record with real time and latencies taken out, for comparing two runs."""
    if isinstance(record, Mapping):
        return {k: stable(v) for k, v in record.items() if k not in VOLATILE}
    if isinstance(record, list):
        return [stable(v) for v in record]
    return record


def string_leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from string_leaves(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from string_leaves(v)


def world_review_texts(root: Path) -> list[str]:
    conn = sqlite3.connect(root / "world.sqlite")
    try:
        return [str(r[0]) for r in conn.execute("SELECT text FROM reviews")]
    finally:
        conn.close()


def decoded(value: Any) -> Any:
    """An exported attribute value: Langfuse stores JSON for anything not a plain str."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if isinstance(value, tuple):
        return list(value)
    return value


@dataclass
class Timings:
    """Wall time of every call the Tracer makes into the dashboard sink."""

    calls: list[tuple[str, float]] = field(default_factory=list)

    def worst(self, *names: str) -> tuple[str, float]:
        picked = [c for c in self.calls if c[0] in names]
        return max(picked, key=lambda c: c[1]) if picked else ("none", 0.0)


class TimedView:
    """Wraps a run view and times each call. Also a LiveSink, as the view is."""

    def __init__(self, inner: Any, timings: Timings) -> None:
        self._inner = inner
        self._timings = timings

    def _timed(self, name: str, fn: Callable[[], None]) -> None:
        started = time.perf_counter()
        try:
            fn()
        finally:
            self._timings.calls.append((name, (time.perf_counter() - started) * 1000))

    def write(self, record: Mapping[str, Any], line: str) -> None:
        self._timed("write", lambda: self._inner.write(record, line))

    def close(self) -> None:
        self._timed("close", self._inner.close)

    def begin(
        self, open_id: str, run_id: str, parent_span_id: str | None, kind: str, name: str
    ) -> None:
        self._timed("begin", lambda: self._inner.begin(open_id, run_id, parent_span_id, kind, name))

    def bind(self, open_id: str, span_id: str) -> None:
        self._timed("bind", lambda: self._inner.bind(open_id, span_id))

    def abandon(self, open_id: str, error: str) -> None:
        self._timed("abandon", lambda: self._inner.abandon(open_id, error))


class TimedSink:
    """A process sink whose run views are timed."""

    def __init__(self, inner: ProcessSink, timings: Timings) -> None:
        self.inner = inner
        self.timings = timings

    def for_run(self, run_id: str, thread_id: str) -> Sink:
        return TimedView(self.inner.for_run(run_id, thread_id), self.timings)

    def status(self) -> dict[str, Any]:
        return self.inner.status()

    def shutdown(self) -> None:
        self.inner.shutdown()
