"""The quarantined reader through the real API (architecture 11, 10.6, 15.1).

Each test drives the app the way the web client does (POST a message, read the run's
SSE stream, read the transcript and state) and then checks the three records. The
scripted reader is one `ScriptedChatModel` shared across the runs of a test, so the
test can read every request the reader received.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from errand.api.runs import ModelFactory
from errand.config import LimitSettings
from errand.llm.anthropic import request_sha256
from errand.llm.scripted import Policy, ScriptedChatModel, ScriptedRequest
from errand.quarantine.reader import SYSTEM_PROMPT, reader_model_key
from errand.tools.sanitize import clean_review
from errand.world.catalog import INJECTION_REVIEWS
from errand.world.scenario import ScenarioConfig, WorldConfig
from evals import invariants
from evals.policies import reader as doubles
from evals.policies import reference
from evals.policies.common import answer, read, tool_calls
from evals.runner import Harness, harness

OPENER = "Find a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
AGAIN = "show me those options again"


def world(seed: int = 14, **kw: Any) -> ScenarioConfig:
    return ScenarioConfig(id="reader_api", seed=seed, world=WorldConfig(**kw))


class Shared:
    """One scripted reader for every run of a test, so its requests can be inspected."""

    def __init__(self, policy: Policy = doubles.keyword) -> None:
        self.model = ScriptedChatModel(script=policy)

    def factory(self) -> ModelFactory:
        return lambda: self.model

    @property
    def requests(self) -> tuple[ScriptedRequest, ...]:
        return self.model.requests


def of_type(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in records if r["type"] == kind]


async def run_records(h: Harness, run_id: str) -> list[dict[str, Any]]:
    rec = await h.records()
    return rec.traces[run_id]


def cache_rows(root: Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(root / "agent.sqlite")
    try:
        return list(
            conn.execute(
                "SELECT provider_id, reviews_sha256, reader_model, output_json FROM reader_cache"
            )
        )
    finally:
        conn.close()


def tool_results(records: list[dict[str, Any]], tool: str) -> list[Any]:
    """The results the planner was given for `tool`, parsed from <tool_data>, once each
    (every later request in a loop repeats the earlier ones)."""
    found: dict[str, Any] = {}
    for call in of_type(records, "model.call"):
        names: dict[str, str] = {}
        for m in call["request"]["messages"]:
            for block in m["content"] if isinstance(m["content"], list) else []:
                if block.get("type") == "tool_use":
                    names[block["id"]] = block["name"]
                if block.get("type") == "tool_result" and names.get(block["tool_use_id"]) == tool:
                    content = block["content"]
                    text = content if isinstance(content, str) else content[0]["text"]
                    body = text.removeprefix("<tool_data>").removesuffix("</tool_data>")
                    found.setdefault(block["tool_use_id"], json.loads(body))
    return list(found.values())


# ---------------------------------------------------------------- the request


async def test_the_reader_request_has_no_tools_and_reviews_only_in_the_user_turn(
    tmp_path: Path,
) -> None:
    shared = Shared()
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    assert len(shared.requests) == 4
    p9 = clean_review(next(t for pid, _, t in INJECTION_REVIEWS if pid == "P9"))
    reads = of_type(records, "reader.call")
    called = [r for r in reads if not r["cache_hit"]]
    for req, record in zip(shared.requests, called, strict=True):
        canon = req.canonical
        assert set(canon) == {
            "max_tokens",
            "messages",
            "model",
            "output_config",
            "system",
            "thinking",
        }
        assert "tools" not in canon and "tool_choice" not in canon and "temperature" not in canon
        assert req.tool_names == ()
        assert canon["model"] == "claude-haiku-5-5"
        assert canon["max_tokens"] == 1500
        assert canon["thinking"] == {"type": "adaptive"}
        assert canon["output_config"]["effort"] == "low"
        assert canon["output_config"]["format"]["type"] == "json_schema"
        assert canon["system"] == SYSTEM_PROMPT  # byte-identical on every call
        (user,) = canon["messages"]
        assert user["role"] == "user" and user["content"].startswith("<reviews>[")
        # The hash in the trace is the hash of what the transport received.
        assert record["request_sha256"] == req.sha256 == request_sha256(canon)
        assert record["reader_model"] == reader_model_key("claude-haiku-5-5")
    texts = [r.canonical["messages"][0]["content"] for r in shared.requests]
    assert any(p9 in t for t in texts)
    # Review text lives in the user turn only, and never in a trace record of the read.
    catalogue = sorted(set(invariants.load_records(tmp_path, []).catalog_reviews))
    needles = {n for t in catalogue for n in invariants.review_needles(t)}
    assert not any(n in SYSTEM_PROMPT for n in needles)
    blob = json.dumps(reads, ensure_ascii=False)
    assert not [n for n in needles if n in blob]


def history_first(request: ScriptedRequest) -> AIMessage:
    """Opens the user's past electrician (E4, never enriched) before searching, so the
    planner's own get_provider makes a real reader call, then searches and answers."""
    view = read(request)
    last = view.loop[-1][0] if view.loop else None
    if last is None:
        return tool_calls(request, "History first.", ("list_bookings", {}))
    if last == "list_bookings":
        return tool_calls(request, "Open E4.", ("get_provider", {"provider_id": "E4"}))
    if last == "get_provider":
        patch = reference.extract(view.current, view)
        return tool_calls(request, "Brief.", ("update_brief", reference.update_brief_args(patch)))
    if last == "update_brief":
        return tool_calls(request, "Search.", ("search_providers", {}))
    return answer(request, "Prakash Plumbing looks best for the morning slot.")


async def test_no_sse_token_carries_reader_output(tmp_path: Path) -> None:
    # The hostile double's raw answer has a free-text summary: a distinctive string that
    # must not reach the stream as a token, or as anything else. Both kinds of read
    # make a real call here: the planner's own get_provider (E4) and the enrichment.
    canary = "Customers love this provider"
    async with harness(tmp_path, world(), policy=history_first, reader=doubles.hostile) as h:
        view = await h.say(OPENER)
    tokens = [e.data["text"] for e in view.events if e.event == "token"]
    assert tokens, "the planner's answer should stream, or this test proves nothing"
    streamed = "".join(tokens)
    assert canary not in streamed
    assert "injection_suspected" not in streamed and "review_id" not in streamed
    assert not [e for e in view.events if canary in json.dumps(e.data, ensure_ascii=False)]
    reads = [e.data for e in view.events if e.data.get("type") == "reader.call"]
    assert (reads[0]["provider_id"], reads[0]["error"]) == ("E4", "schema_violation")
    # E4 used one of the run's four reads, so enrichment had three left (K = 3).
    assert [r["error"] for r in reads[1:]] == ["schema_violation"] * 3
    assert view.events[-1].data["totals"]["reader_calls"] == 4


# ---------------------------------------------------------------- cache and cap


async def test_a_cache_hit_makes_no_reader_call_on_the_next_turn(tmp_path: Path) -> None:
    shared = Shared()
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        first = await h.say(OPENER)
        second = await h.say(AGAIN)
        rec = await h.records()
    assert first.outcome == second.outcome == "PRESENTED_OPTIONS"
    assert len(shared.requests) == 4  # all from the first turn
    reads = of_type(rec.traces[second.run_id], "reader.call")
    ranker = [r for r in of_type(rec.traces[second.run_id], "tool.call") if r["caller"] == "ranker"]
    assert len(ranker) == 4 and reads and all(r["cache_hit"] for r in reads)
    assert all(r["cost_usd"] == 0 and r["usage"] is None for r in reads)
    (end,) = of_type(rec.traces[second.run_id], "run.end")
    assert end["totals"]["reader_calls"] == 0
    assert len(cache_rows(tmp_path)) == 4


def open_fifth(request: ScriptedRequest) -> AIMessage:
    """The reference planner, but after the search it opens the fifth candidate."""
    view = read(request)
    if view.loop and view.loop[-1][0] == "search_providers" and not view.loop[-1][2]:
        fifth = view.loop[-1][1]["candidates"][4]["provider_id"]
        return tool_calls(request, "Open the fifth.", ("get_provider", {"provider_id": fifth}))
    if view.loop and view.loop[-1][0] == "get_provider":
        return answer(request, "Here they are.")
    return reference.policy(request)


async def test_the_fifth_read_in_a_run_is_refused(tmp_path: Path) -> None:
    async with harness(tmp_path, world(seed=15), policy=open_fifth) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    reads = of_type(records, "reader.call")
    assert [r["error"] for r in reads] == [None, None, None, None, "cap_reached"]
    assert reads[-1]["usage"] is None and not reads[-1]["cache_hit"]
    (end,) = of_type(records, "run.end")
    assert end["totals"]["reader_calls"] == 4
    (provider,) = tool_results(records, "get_provider")
    assert provider["digest"] is None
    assert "reviews" not in provider and "about" not in provider


async def test_a_read_is_skipped_when_less_time_is_left_than_the_reader_timeout(
    tmp_path: Path,
) -> None:
    limits = LimitSettings(run_deadline_s=15)
    async with harness(tmp_path, world(), limits=limits) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    assert {r["error"] for r in of_type(records, "reader.call")} == {"deadline"}
    assert of_type(records, "run.end")[0]["totals"]["reader_calls"] == 0


async def test_a_read_is_skipped_once_the_run_spent_its_cost_cap(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), limits=LimitSettings(run_cost_cap_usd=0.0)) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    assert {r["error"] for r in of_type(records, "reader.call")} == {"cost_cap"}


# ---------------------------------------------------------------- failing closed


async def test_a_hostile_readers_output_is_fully_dropped(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), reader=doubles.hostile) as h:
        view = await h.say(OPENER)
        state = (await h.conversation())["state"]
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    reads = of_type(records, "reader.call")
    assert all(r["labels"] is None for r in reads)
    assert cache_rows(tmp_path) == []
    candidates = state["errand"]["shortlist"]["candidates"]
    assert all(c["digest"] is None and c["score_parts"]["review_signal"] == 0 for c in candidates)
    card = next(m["body"] for m in view.messages if m["body"]["type"] == "shortlist")
    assert all(
        o["injection_flags"] == 0
        and o["notice"] is None
        and "unverified_reviews" not in o["badges"]
        for o in card["candidates"]
    )
    (provider,) = tool_results(records, "get_provider")
    assert provider["digest"] is None


def table_rows(root: Path, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "agent.sqlite")
    try:
        return list(conn.execute(f"SELECT * FROM {table}"))
    finally:
        conn.close()


async def test_a_refusal_is_never_cached_and_never_sent_again(tmp_path: Path) -> None:
    shared = Shared(doubles.refusal)
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        first = await h.say(OPENER)
        assert cache_rows(tmp_path) == []
        sent = len(shared.requests)
        assert sent == 4
        second = await h.say(AGAIN)
        rec = await h.records()
    assert first.outcome == second.outcome == "PRESENTED_OPTIONS"
    # The refused prompts are marked, not cached; turn 2 sends nothing for them.
    assert len(shared.requests) == sent
    assert cache_rows(tmp_path) == []
    assert len(table_rows(tmp_path, "reader_refusals")) == 4
    reads = of_type(rec.traces[second.run_id], "reader.call")
    assert [r["error"] for r in reads[:4]] == ["not_retried:refusal"] * 4
    assert all(r["labels"] is None and not r["cache_hit"] for r in reads)
    assert of_type(rec.traces[second.run_id], "run.end")[0]["totals"]["reader_calls"] == 0


async def test_a_changed_reader_model_key_tries_a_refused_prompt_again(tmp_path: Path) -> None:
    shared = Shared(doubles.refusal)
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        await h.say(OPENER)
        sent = len(shared.requests)
        conn = sqlite3.connect(tmp_path / "agent.sqlite")
        with conn:
            conn.execute("UPDATE reader_refusals SET reader_model = 'claude-haiku-5-5/r0-0'")
        conn.close()
        again = await h.say(AGAIN)
        rec = await h.records()
    assert len(shared.requests) == sent + 4
    reads = of_type(rec.traces[again.run_id], "reader.call")
    assert [r["error"] for r in reads[:4]] == ["refusal"] * 4


async def test_timeouts_and_transport_errors_are_not_marked_and_may_be_retried(
    tmp_path: Path,
) -> None:
    shared = Shared(doubles.timeout)
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        await h.say(OPENER)
        sent = len(shared.requests)
        await h.say(AGAIN)
    assert table_rows(tmp_path, "reader_refusals") == []
    assert len(shared.requests) > sent


async def test_max_tokens_fails_closed_end_to_end_and_caches_nothing(tmp_path: Path) -> None:
    shared = Shared(doubles.max_tokens)
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
        state = (await h.conversation())["state"]
    assert view.outcome == "PRESENTED_OPTIONS"
    reads = of_type(records, "reader.call")
    assert [r["error"] for r in reads[:4]] == ["max_tokens"] * 4
    assert all(r["labels"] is None for r in reads)
    assert cache_rows(tmp_path) == [] and table_rows(tmp_path, "reader_refusals") == []
    candidates = state["errand"]["shortlist"]["candidates"]
    assert candidates and all(c["score_parts"]["review_signal"] == 0 for c in candidates)
    assert all(c.get("digest") is None for c in candidates)


async def test_the_cache_key_hashes_raw_reviews_so_a_zero_width_edit_misses(
    tmp_path: Path,
) -> None:
    # Two raw payloads that clean to the same text must not share an entry: an update
    # that only inserts an invisible character would otherwise be missed.
    shared = Shared()
    async with harness(tmp_path, world(), reader_factory=shared.factory()) as h:
        await h.say(OPENER)
        before = cache_rows(tmp_path)
        sent = len(shared.requests)
        conn = sqlite3.connect(tmp_path / "world.sqlite")
        with conn:
            (rid, text) = conn.execute(
                "SELECT review_id, text FROM reviews WHERE provider_id = 'P12' "
                "ORDER BY review_id LIMIT 1"
            ).fetchone()
            conn.execute("UPDATE reviews SET text = ? WHERE review_id = ?", (text + "\u200b", rid))
        conn.close()
        assert clean_review(text + "\u200b") == clean_review(text)
        second = await h.say(AGAIN)
        rec = await h.records()
    p12 = [
        r for r in of_type(rec.traces[second.run_id], "reader.call") if r["provider_id"] == "P12"
    ]
    assert p12 and not p12[0]["cache_hit"]
    assert len(shared.requests) == sent + 1  # only the edited provider is read again
    after = cache_rows(tmp_path)
    assert len(after) == len(before) + 1
    assert len({row[1] for row in after if row[0] == "P12"}) == 2


async def test_a_cache_store_error_costs_the_digest_not_the_run(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        conn = sqlite3.connect(tmp_path / "agent.sqlite")
        with conn:
            conn.execute("DROP TABLE reader_cache")
        conn.close()
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    reads = of_type(records, "reader.call")
    assert reads and all(r["cache_error"] in ("lookup", "put") for r in reads)
    assert any(r["labels"] for r in reads)  # the answer was still used


async def test_concurrent_timeouts_cost_the_run_one_timeout_not_four(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), reader=doubles.timeout) as h:
        view = await h.say(OPENER)
        records = await run_records(h, view.run_id)
    assert view.outcome == "PRESENTED_OPTIONS"
    reads = of_type(records, "reader.call")[:4]
    assert [r["error"] for r in reads] == ["timeout"] * 4
    # Sim time starts at 08:00:00; the four reads waited side by side for 20 s.
    assert {r["ts"][11:19] for r in reads} == {"08:00:20"}


async def test_an_entry_under_another_prompt_version_is_not_served(tmp_path: Path) -> None:
    poison = json.dumps(
        {
            "labels": [
                {
                    "review_id": rid,
                    "sentiment": "negative",
                    "themes": [],
                    "injection_suspected": True,
                }
                for rid in [f"R-P12-{n}" for n in range(1, 6)]
            ]
        }
    )
    async with harness(tmp_path, world()) as h:
        await h.say(OPENER)
        conn = sqlite3.connect(tmp_path / "agent.sqlite")
        with conn:
            conn.execute(
                "UPDATE reader_cache SET output_json = ?, reader_model = ? "
                "WHERE provider_id = 'P12'",
                (poison, "claude-haiku-5-5/r0-00000000"),
            )
        conn.close()
        stale = await h.say(AGAIN)
        conn = sqlite3.connect(tmp_path / "agent.sqlite")
        with conn:
            conn.execute(
                "UPDATE reader_cache SET output_json = ? WHERE provider_id = 'P12' "
                "AND reader_model = ?",
                (poison, reader_model_key("claude-haiku-5-5")),
            )
        conn.close()
        current = await h.say("show me those again please")
        rec = await h.records()

    def p12(run_id: str) -> dict[str, Any]:
        return next(
            r for r in of_type(rec.traces[run_id], "reader.call") if r["provider_id"] == "P12"
        )

    # The old-version row is ignored: P12 is read again and comes back clean.
    assert not p12(stale.run_id)["cache_hit"]
    assert not any(lb["injection_suspected"] for lb in p12(stale.run_id)["labels"])
    # Control: the same poison under the current key is served, so the key is what
    # kept the stale entry out.
    assert p12(current.run_id)["cache_hit"]
    assert any(lb["injection_suspected"] for lb in p12(current.run_id)["labels"])


# ---------------------------------------------------------------- order and cost


async def test_handles_on_the_card_follow_the_ranked_order_the_planner_saw(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        view = await h.say(OPENER)
        state = (await h.conversation())["state"]
        records = await run_records(h, view.run_id)
        op_log = await h.op_log()
    card = next(m["body"] for m in view.messages if m["body"]["type"] == "shortlist")
    on_card = [(c["provider_id"], [s["ref"] for s in c["slots"]]) for c in card["candidates"]]
    (search,) = tool_results(records, "search_providers")
    seen = [(c["provider_id"], [s["ref"] for s in c["slots"]]) for c in search["candidates"]]
    assert on_card[: len(seen)] == seen
    refs = [ref for _, rs in on_card for ref in rs]
    assert refs == [f"S{i}" for i in range(1, len(refs) + 1)]
    scores = [c["score"] for c in state["errand"]["shortlist"]["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert [c["score"] for c in search["candidates"]] == scores[: len(seen)]
    # Not vacuous: the ranked order is not the order the world listed them in.
    (row,) = [r for r in op_log if r["op"] == "search_providers"]
    world_order = [p["provider_id"] for p in json.loads(row["response_json"])["providers"]]
    assert [pid for pid, _ in on_card] != [p for p in world_order if p in dict(on_card)]


async def test_reader_cost_is_in_run_end_and_reader_calls_are_never_model_calls(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world()) as h:
        view = await h.say(OPENER)
        rec = await h.records()
    records = rec.traces[view.run_id]
    calls = of_type(records, "model.call")
    reads = [r for r in of_type(records, "reader.call") if r["cost_usd"]]
    (end,) = of_type(records, "run.end")
    assert {c["model"] for c in calls} == {"claude-opus-5-5"}
    assert end["totals"]["model_calls"] == len(calls)
    assert end["totals"]["reader_calls"] == 4 and len(reads) == 4
    planner = sum(Decimal(str(c["cost_usd"])) for c in calls)
    reader = sum(Decimal(str(r["cost_usd"])) for r in reads)
    assert reader > 0
    assert Decimal(str(end["totals"]["cost_usd"])) == planner + reader
    assert rec.runs[0]["cost_usd"] == end["totals"]["cost_usd"]
    tokens = sum(c["usage"]["input_uncached"] for c in calls) + sum(
        r["usage"]["input_uncached"] for r in reads
    )
    assert end["totals"]["tokens_in"] == tokens
