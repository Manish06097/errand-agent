"""scripts/langfuse_check.py compares Langfuse with the JSONL; offline, on canned answers.

The live run (A5) needs Docker. These pin the comparison itself: an abandoned call (a
model or tool call that raised, so the sink ended it at ERROR with no record behind it)
is reported, not counted as a generation or a span, and does not fail a run whose
mirror is otherwise exact.
"""

from __future__ import annotations

from typing import Any

from tests.dev.test_langfuse_stack import load_script

THREAD = "c_0001"
TRACE = "0" * 31 + "1"


class CannedApi:
    """Answers the observations query with one page."""

    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self.observations = observations
        self.queries: list[dict[str, Any]] = []

    def get(self, path: str, **query: Any) -> Any:
        assert path == "/api/public/v2/observations"
        self.queries.append(query)
        return {"data": self.observations, "meta": {}}


def obs(kind: str, span_id: str | None, **extra: Any) -> dict[str, Any]:
    o: dict[str, Any] = {"type": kind, "name": kind.lower(), "sessionId": THREAD, **extra}
    if span_id is not None:
        o["metadata"] = {"span_id": span_id, "seq": int(span_id[2:])}
    return o


def run(check: Any, **overrides: Any) -> Any:
    fields = {
        "run_id": "r_0001",
        "trace_id": TRACE,
        "thread_id": THREAD,
        "cost_usd": 0.0123,
        "model_calls": 1,
        "outcome": "REPLIED",
        "spans": 4,
    }
    fields.update(overrides)
    return check.Run(**fields)


def mirrored() -> list[dict[str, Any]]:
    return [
        obs("AGENT", "s_1", isRootObservation=True),
        obs("CHAIN", "s_2"),
        obs("GENERATION", "s_3", totalCost=0.0123, level="DEFAULT"),
        obs("EVENT", "s_4"),
    ]


def test_an_exact_mirror_passes_and_asks_for_metadata() -> None:
    check = load_script("langfuse_check")
    api = CannedApi(mirrored())
    result = check.check(api, run(check))
    assert result.problems == []
    assert result.seen["abandoned"] == 0
    assert "metadata" in api.queries[0]["fields"].split(",")


def test_an_abandoned_call_is_reported_and_left_out_of_the_comparison() -> None:
    check = load_script("langfuse_check")
    abandoned = obs("GENERATION", None, level="ERROR", statusMessage="APIStatusError")
    abandoned_tool = obs("TOOL", None, level="ERROR", statusMessage="ToolTimeout")
    result = check.check(CannedApi([*mirrored(), abandoned, abandoned_tool]), run(check))
    assert result.problems == []
    assert result.seen["abandoned"] == 2
    assert result.seen["generations"] == 1
    assert result.seen["observations"] == 4


def test_a_mismatch_still_fails() -> None:
    check = load_script("langfuse_check")
    # An observation with no span id that is not at ERROR is not an abandoned call.
    stray = obs("SPAN", None, level="DEFAULT")
    assert check.check(CannedApi([*mirrored(), stray]), run(check)).problems
    # A second generation that carries a span id is a real extra generation.
    extra = obs("GENERATION", "s_5", totalCost=0.0, level="DEFAULT")
    problems = check.check(CannedApi([*mirrored(), extra]), run(check)).problems
    assert any("generations" in p for p in problems)
    # Our cost, not another one.
    assert check.check(CannedApi(mirrored()), run(check, cost_usd=0.02)).problems
