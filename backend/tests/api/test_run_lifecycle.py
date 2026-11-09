"""Every way a run ends, through the real API: a templated message and one run.end.

Also: the brief survives an API restart (it lives in the checkpoint), and a restarted
process never reuses a run id.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import AIMessage

from errand.api.app import AppConfig, create_app, scripted_factory
from errand.config import LimitSettings, ModelSettings
from errand.llm.scripted import ScriptedRequest
from errand.tools.runtime import in_process
from errand.world.engine import WorldHost
from errand.world.scenario import ScenarioConfig, WorldConfig
from evals import invariants
from evals.policies import reference
from evals.policies.common import read, tool_calls
from evals.runner import harness
from tests.api.support import OPENER, world


def outcomes(rec: invariants.Records) -> list[str]:
    return [r["outcome"] for r in rec.runs]


async def test_an_exception_in_a_node_still_ends_with_a_message_and_run_end(
    tmp_path: Path,
) -> None:
    def broken(request: ScriptedRequest) -> AIMessage:
        raise RuntimeError("the planner fell over")

    async with harness(tmp_path, world(), policy=broken) as h:
        view = await h.say(OPENER)
        rec = await h.records()
    assert view.outcome == "INTERNAL_ERROR"
    notice = view.notice()
    assert notice is not None and "Nothing was booked or changed" in notice["text"]
    assert view.run_id in notice["text"]
    assert invariants.inv07_one_end_per_run(rec) == []
    errors = [r for r in rec.traces[view.run_id] if r["type"] == "error"]
    assert [e["where"] for e in errors] == ["llm.gateway", "run_manager"]


async def test_an_unmapped_stop_reason_is_internal_error_until_m13(tmp_path: Path) -> None:
    def truncated(request: ScriptedRequest) -> AIMessage:
        message = reference.policy(request)
        message.response_metadata["stop_reason"] = "max_tokens"
        return message

    async with harness(tmp_path, world(), policy=truncated) as h:
        view = await h.say(OPENER)
    assert view.outcome == "INTERNAL_ERROR"


async def test_a_message_over_its_cap_is_refused_without_a_model_call(tmp_path: Path) -> None:
    async with harness(tmp_path, world()) as h:
        view = await h.say("fix my tap " * 5_000)
        rec = await h.records()
    assert view.outcome == "CONTEXT_TOO_LARGE"
    types = Counter(r["type"] for r in rec.traces[view.run_id])
    assert types["model.call"] == 0
    built = next(r for r in rec.traces[view.run_id] if r["type"] == "context.built")
    assert built["manifest"]["over_cap"] == "current" and built["manifest"]["sent"] is False


def looping(calls_per_turn: int) -> Any:
    def policy(request: ScriptedRequest) -> AIMessage:
        return tool_calls(request, "Again.", *[("list_bookings", {})] * calls_per_turn)

    return policy


async def test_the_iteration_cap_ends_limit_reached(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), policy=looping(1)) as h:
        view = await h.say(OPENER)
        rec = await h.records()
    assert view.outcome == "LIMIT_REACHED"
    assert rec.runs[0]["model_calls"] == 6 and rec.runs[0]["tool_calls"] == 6


async def test_a_limit_notice_shows_what_is_known_and_claims_nothing_else(
    tmp_path: Path,
) -> None:
    def record_then_loop(request: ScriptedRequest) -> AIMessage:
        view = read(request)
        if not view.loop:
            patch = reference.extract(view.current, view)
            return tool_calls(
                request, "Record it.", ("update_brief", reference.update_brief_args(patch))
            )
        return tool_calls(request, "Again.", ("list_bookings", {}))

    async with harness(tmp_path, world(), policy=record_then_loop) as h:
        view = await h.say(OPENER)
        state = (await h.conversation())["state"]
    assert view.outcome == "LIMIT_REACHED"
    notice = view.notice()
    assert notice is not None
    window = state["errand"]["brief"]["window"]["value"]["label"]
    assert notice["text"] == (
        "I hit my step limit for this turn, so I stopped. "
        f"So far I have: plumber, {window}, under ₹1,500. Nothing was booked."
    )


async def test_a_limit_notice_with_nothing_known_says_only_that_it_stopped(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world(), policy=looping(1)) as h:
        view = await h.say("hello")
    notice = view.notice()
    assert view.outcome == "LIMIT_REACHED" and notice is not None
    assert notice["text"] == "I hit my step limit for this turn, so I stopped. Nothing was booked."


async def test_a_search_that_finds_nobody_is_a_code_template_not_an_empty_shortlist(
    tmp_path: Path,
) -> None:
    """The no-fit template (M8): REPLIED, the errand stays COMPARING with an empty
    shortlist, and code names what could widen, window then budget then area."""
    async with harness(tmp_path, world()) as h:
        await h.say("Find a plumber for my kitchen tap this week, mornings only, under ₹100.")
        view = await h.say("indiranagar")
        op_log = await h.op_log()
        state = (await h.conversation())["state"]
    assert [r["op"] for r in op_log].count("search_providers") == 1
    assert state["errand"]["shortlist"]["candidates"] == []
    assert state["errand"]["phase"] == "comparing"
    assert view.outcome == "REPLIED"
    assert [m["body"]["type"] for m in view.messages] == ["notice"]
    notice = view.messages[0]["body"]
    assert notice["text"] == (
        "No provider has a free slot for plumber, Indiranagar, Mon 9 Nov - Sun 15 Nov, "
        "07:00-12:00, under \u20b9100. I can widen the dates or times, your budget or the area. "
        "Which would you like?"
    )
    assert [(a["kind"], a["label"], a["reply"]) for a in notice["actions"]] == [
        ("quick_reply", "Any time of day", "this week, any time of day"),
        ("quick_reply", "Raise budget to \u20b9500", "up to \u20b9500"),
        ("quick_reply", "Try another area", "try a different area"),
    ]


async def test_the_tool_call_cap_ends_limit_reached(tmp_path: Path) -> None:
    async with harness(tmp_path, world(), policy=looping(5)) as h:
        view = await h.say(OPENER)
        rec = await h.records()
        op_log = await h.op_log()
    assert view.outcome == "LIMIT_REACHED"
    assert rec.runs[0]["tool_calls"] == 16 and len(op_log) == 16


async def test_the_simulated_deadline_caps_timeouts_and_ends_limit_reached(
    tmp_path: Path,
) -> None:
    # Each call takes 4.5 s of world time, inside the 5 s read timeout. The 14th starts
    # at 58.5 s with 1.5 s of run time left, so its timeout is capped to 1.5 s and it
    # times out; the retry finds the deadline passed and nothing more is sent.
    slow = ScenarioConfig(id="slow", seed=7, world=WorldConfig(latency_ms={"default": 4_500}))
    async with harness(tmp_path, slow, policy=looping(5)) as h:
        view = await h.say(OPENER)
        op_log = await h.op_log()
        rec = await h.records()
    assert view.outcome == "LIMIT_REACHED"
    assert len(op_log) == 14
    calls = [r for r in rec.traces[view.run_id] if r["type"] == "tool.call"]
    assert [c["error"] for c in calls][-2:] == [None, "ToolTimeout"]


@asynccontextmanager
async def api(root: Path, host: WorldHost) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        AppConfig(
            data_dir=root,
            world=in_process(host),
            model_factory=scripted_factory(reference.policy),
            model_settings=ModelSettings(model_mode="scripted"),
            limits=LimitSettings(),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        yield client


async def say(client: httpx.AsyncClient, conversation: str, text: str, msg_id: str) -> str:
    posted = await client.post(
        f"/api/conversations/{conversation}/messages",
        json={"client_msg_id": msg_id, "text": text},
    )
    run_id = str(posted.json()["run_id"])
    await client.get(f"/api/runs/{run_id}/events")  # returns once the run has finished
    return run_id


async def test_the_brief_survives_an_api_restart_and_run_ids_move_on(tmp_path: Path) -> None:
    host = WorldHost(tmp_path / "world.sqlite")
    host.load(world("restart"), "sim")
    try:
        async with api(tmp_path, host) as client:
            conversation = (await client.post("/api/conversations")).json()["conversation_id"]
            assert await say(client, conversation, "need a plumber", "a") == "r_0001"
            assert await say(client, conversation, "indiranagar", "b") == "r_0002"
        # A new process: new graph, new checkpointer connection, new id counters.
        async with api(tmp_path, host) as client:
            before = (await client.get(f"/api/conversations/{conversation}")).json()
            brief = before["state"]["errand"]["brief"]
            assert brief["service"]["value"] == "plumber"
            assert brief["area"]["value"] == "indiranagar"
            run_id = await say(client, conversation, "this week mornings, 1500", "c")
            assert run_id == "r_0003"
            after = (await client.get(f"/api/conversations/{conversation}")).json()
    finally:
        host.close()
    assert [r["outcome"] for r in after["runs"]] == [
        "ASKED_USER",
        "ASKED_USER",
        "PRESENTED_OPTIONS",
    ]
