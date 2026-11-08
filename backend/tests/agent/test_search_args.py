"""The model cannot steer the world: search arguments come from the brief, never the model.

A hostile scripted planner sends search_providers arguments for another service, area
and dates, searches before the brief is complete, opens providers that are on no
shortlist and asks for `book`. The assertions read the world's op_log, the ground
truth, not just the agent's trace.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from evals.policies.hostile import arg_smuggling_policy, stale_shortlist_policy
from evals.runner import expected_search_args, harness
from tests.api.support import world

MESSAGE = "Find a plumber in Indiranagar this week, mornings, under ₹1,500."


async def test_every_executed_search_is_the_resolved_brief(tmp_path: Path) -> None:
    async with harness(tmp_path, world("search_args"), policy=arg_smuggling_policy) as h:
        view = await h.say(MESSAGE)
        state = (await h.conversation())["state"]
        op_log = await h.op_log()
        records = await h.records()

    assert view.outcome == "PRESENTED_OPTIONS"
    brief = expected_search_args(state)
    assert brief == {
        "service": "plumber",
        "area": "indiranagar",
        "date_range": {"from": "2026-11-09", "to": "2026-11-15"},
    }
    searches = [r for r in op_log if r["op"] == "search_providers"]
    # Five search_providers calls were made; only the one after the brief was complete
    # and without arguments reached the world, with the brief's values.
    assert len(searches) == 1
    assert json.loads(searches[0]["args_json"]) == brief
    # The only get_provider calls are code's: the ranker opening the top shortlisted
    # candidates for the review reader (M10). None for E15, P19 or E4, and no book.
    ops = Counter(r["op"] for r in op_log)
    assert ops == {"search_providers": 1, "get_provider": 4}, ops
    shortlisted = {c["provider_id"] for c in state["errand"]["shortlist"]["candidates"]}
    opened = {
        json.loads(r["args_json"])["provider_id"] for r in op_log if r["op"] == "get_provider"
    }
    assert opened <= shortlisted and not opened & {"E15", "P19", "E4"}

    trace = [r for recs in records.traces.values() for r in recs]
    rejected = [r["detail"] for r in trace if r["type"] == "guard.planner_tool_rejected"]
    assert [(d["tool"], d["reason"]) for d in rejected] == [
        ("search_providers", "arguments_not_allowed"),
        ("get_provider", "not_in_shortlist_or_bookings"),
        ("book", "not_allowed"),
        ("search_providers", "arguments_not_allowed"),
        ("get_provider", "not_in_shortlist_or_bookings"),
        ("get_provider", "not_in_shortlist_or_bookings"),
    ]
    tool_calls = [r for r in trace if r["type"] == "tool.call" and r["caller"] == "planner"]
    assert [(r["tool"], r["args"]) for r in tool_calls] == [("search_providers", brief)]
    assert tool_calls[0]["world_seq"] == searches[0]["seq"]
    ranker = [r for r in trace if r["type"] == "tool.call" and r["caller"] == "ranker"]
    assert {r["tool"] for r in ranker} == {"get_provider"} and len(ranker) == 4


async def test_search_with_a_missing_field_is_a_tool_error_with_no_world_call(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world("search_incomplete"), policy=arg_smuggling_policy) as h:
        view = await h.say("Find me a plumber this week, mornings, under ₹1,500.")
        op_log = await h.op_log()
        records = await h.records()

    assert [r["op"] for r in op_log] == []
    loop_results = [
        r
        for recs in records.traces.values()
        for r in recs
        if r["type"] == "model.call" and r["request"]["messages"][-1]["role"] == "user"
    ]

    def results(call: int) -> list[str]:
        blocks = loop_results[call]["request"]["messages"][-1]["content"]
        return [b["content"] for b in blocks if b["type"] == "tool_result"]

    # Before any patch: the smuggled search is refused, the bare one names every gap.
    assert '"error":"no_arguments"' in results(1)[0]
    missing_all = '"missing":["service","area","window","budget_max_inr"]'
    assert '"error":"brief_incomplete",' + missing_all in results(1)[1]
    # After the patch only the area is missing, and the search still never runs.
    assert '"error":"brief_incomplete","missing":["area"]' in results(3)[1]
    assert view.outcome == "REPLIED"


async def test_get_provider_refuses_a_shortlist_from_an_earlier_brief(tmp_path: Path) -> None:
    async with harness(tmp_path, world("stale_shortlist"), policy=stale_shortlist_policy) as h:
        first = await h.say(MESSAGE)
        after_search = await h.op_log()
        await h.say("Actually keep it under ₹1,000.")
        op_log = await h.op_log()
        records = await h.records()

    assert first.outcome == "PRESENTED_OPTIONS"
    # The four get_provider calls are the ranker's, for the review reader (M10).
    assert Counter(r["op"] for r in after_search) == {"search_providers": 1, "get_provider": 4}
    # Turn 2: the first get_provider (shortlist current) reached the world; the second,
    # after the budget change made the shortlist stale, did not.
    turn2 = op_log[len(after_search) :]
    assert [r["op"] for r in turn2] == ["get_provider"], turn2
    trace = [r for recs in records.traces.values() for r in recs]
    patches = [r for r in trace if r["type"] == "brief.patch" and r["evidence_ok"]]
    assert [p["fields"]["set"] for p in patches][-1] == ["budget_max_inr"]
    rejected = [r["detail"] for r in trace if r["type"] == "guard.planner_tool_rejected"]
    assert [(d["tool"], d["reason"]) for d in rejected] == [
        ("get_provider", "not_in_shortlist_or_bookings")
    ]
