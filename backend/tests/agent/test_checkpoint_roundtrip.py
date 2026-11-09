"""A fully populated ThreadState survives the real AsyncSqliteSaver with strict msgpack.

One process writes the checkpoints, a fresh process reads them back: every value must
come back equal and as the same types all the way down (not a dict that a later
validation would quietly turn back into a model), with no LangGraph serde warning. The
threads cover every member of the `Inbound`, `PlanResult` and `StepResult` unions,
including M4's `HoldRef` on the errand, the inbound variants M4 sends (a typed grant, a
refresh_hold action, a message marked for the confirm hint), `Proposal`, and `park`.

This is also where the generic-`Fact` trap is pinned: a parametrised `Fact[Service]`
is not restored by a strict checkpoint (it comes back as a dict), so the brief uses
concrete fact classes, and the type walk below fails if anything regresses to one.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # subprocesses or real servers: serial step of make check

SCRIPT = textwrap.dedent(
    """
    import asyncio, logging, sys, warnings
    from datetime import datetime, time, timedelta
    from decimal import Decimal
    from itertools import zip_longest

    import errand  # strict msgpack on before langgraph loads
    from langgraph.checkpoint.serde import _msgpack
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel

    from errand.agent.state import SystemNote, ThreadState
    from errand.clock import IST
    from errand.domain.brief import (
        AreaFact, AskRecord, Brief, BriefField, BudgetFact, ErrandKind, Provenance,
        ProviderPref, ProviderPrefFact, ServiceFact, TextFact, TimeWindow, TimeWindowExpr,
        WindowFact,
    )
    from errand.domain.counters import RunCounters
    from errand.domain.errand import Errand, Phase
    from errand.domain.inbound import (
        ApprovalDeclined, ApprovalGranted, Recover, SystemEvent, UiAction, UserMessage,
    )
    from errand.domain.outcomes import (
        ActionFailed, AlreadyHandled, Ask, AskExhausted, BookCalled, Booked, CardReady,
        ConfirmHint, Declined, Failure, HoldFailed, Limit, Options, OverBudget, PendingCheck,
        Proposal, ProposalRefused, Replan, Reply, Requote, RunOutcome, TooLarge, ToolDown,
    )
    from errand.domain.slots import (
        Candidate, Exclusion, HoldRef, ReviewDigest, Shortlist, SlotHandle,
    )
    from errand.world.contracts import Area, Service, Slot

    seen = []
    logging.basicConfig(level=logging.WARNING)
    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())
    logging.getLogger().addHandler(Catch())
    warnings.simplefilter("error")

    T0 = datetime(2026, 11, 9, 8, 0, tzinfo=IST)
    prov = Provenance(source="user_msg", message_id="r_0001-u", evidence="this week, mornings")
    window = TimeWindow(
        start=T0, end=T0 + timedelta(days=7), daily_from=time(7), daily_to=time(12),
        weekdays=frozenset({1, 2, 3}), label="Mon 9 Nov - Sun 15 Nov, 07:00-12:00",
    )
    expr = TimeWindowExpr(phrase="this week", part_of_day="morning", not_before=time(7),
                          not_after=time(12))
    brief = Brief(
        version=4,
        service=ServiceFact(value=Service.PLUMBER, prov=prov, set_at_turn=1),
        issue=TextFact(value="kitchen tap", prov=prov, set_at_turn=1),
        area=AreaFact(value=Area.INDIRANAGAR, prov=prov, set_at_turn=2),
        window=WindowFact(value=window, expr=expr, prov=prov, set_at_turn=1),
        budget_max_inr=BudgetFact(value=1500, prov=prov, set_at_turn=1),
        provider_pref=ProviderPrefFact(
            value=ProviderPref(prefer_id="E4", avoid_ids=["P9"]),
            prov=Provenance(source="booking_history", booking_id="B-1"), set_at_turn=3),
        target_booking_id=TextFact(value="B-1", prov=Provenance(source="ui_action"),
                                   set_at_turn=3),
        new_window=WindowFact(value=window, prov=Provenance(source="memory", memory_id="m-1"),
                              set_at_turn=3),
        asks=[AskRecord(field=BriefField.AREA, turn=1, with_options=True)],
        ask_exhausted=[BriefField.AREA],
    )
    slot = Slot(start=T0 + timedelta(days=1, hours=1), end=T0 + timedelta(days=1, hours=2))
    handle = SlotHandle(ref="S1", provider_id="P1", slot=slot, search_price_inr=600)
    shortlist = Shortlist(
        brief_version=4, window_label=window.label,
        candidates=[Candidate(
            provider_id="P1", name="Ravi Plumbing", rating=4.6, price_inr=600,
            review_count=12, verified=True, slots=[handle],
            digest=ReviewDigest(n_reviews=3, sentiment_counts={"positive": 2},
                                top_themes=["punctual"], injection_flags=1,
                                reader_model="claude-haiku-5-5"),
            badges=["verified", "unverified_reviews"], score=0.71,
            score_parts={"rating_norm": 0.3},
        )],
        excluded=[Exclusion(provider_id="P2", name="Shiva", reason="over_budget")],
    )
    errand = Errand(
        errand_id="e_r_0001", kind=ErrandKind.BOOK, phase=Phase.COMPARING, origin="user",
        brief=brief, shortlist=shortlist,
        hold=HoldRef(hold_record_id="h1", hold_id="H-1", provider_id="P1", slot=slot,
                     search_price_inr=600, quoted_price_inr=650,
                     expires_at=T0 + timedelta(minutes=10)),
        approval_id="ap_1", intent_id="in_1", replaces_booking_id="B-0",
    )
    inbounds = [
        UserMessage(message_id="r_0001-u", client_msg_id="cm_1", text="hi", turn_no=1),
        UiAction(client_action_id="ca_1", kind="select_slot", payload={"slot_ref": "S1"}),
        ApprovalGranted(approval_id="ap_1", via="button", message_id="m1"),
        ApprovalDeclined(approval_id="ap_1"),
        SystemEvent(kind="followup_start", booking_id="B-1"),
        Recover(intent_id="in_1"),
        ApprovalGranted(approval_id="ap_2", via="typed", message_id="r_0002-u"),
        UiAction(client_action_id="ca_2", kind="refresh_hold", payload={"approval_id": "ap_1"}),
        UserMessage(message_id="r_0003-u", text="ok", turn_no=3, confirm_hint=True),
    ]
    results = [
        Reply(text="hello"), Ask(fields=[BriefField.AREA], question="where?", options=["x"]),
        AskExhausted(still_needed=[BriefField.AREA]), Options(recommendation="S1"),
        Proposal(action="book", provider_id="P1", slot_ref="S1"), Limit(limit="tool_calls"),
        ToolDown(tool="search_providers", error_class="ToolTransient"),
        TooLarge(section="current", estimated=2000, cap=1000), Failure(reason="x"),
    ]
    steps = [
        CardReady(approval_id="ap_1", replaced_reason="hold_lapsed"),
        ProposalRefused(reason="duplicate_booking", text="no"),
        HoldFailed(provider_id="P1", slot_label="Tue 10 Nov, 09:00-10:00"),
        OverBudget(provider_id="P1", provider_name="Ravi", slot_label="x", quoted_price_inr=1800,
                   budget_inr=1500),
        ConfirmHint(approval_id="ap_1"), Declined(approval_id="ap_1"),
        AlreadyHandled(approval_id="ap_1", reason="consumed"),
        Requote(approval_id="ap_1", reason="hold_lapsed"), Replan(note="over_budget"),
        Booked(approval_id="ap_1", intent_id="in_ap_1", booking_id="B-1"),
        ActionFailed(approval_id="ap_1", reason="hold_expired", fresh_quote=True),
        PendingCheck(approval_id="ap_1", intent_id="in_ap_1", reason="status_not_confirmed"),
        BookCalled(approval_id="ap_1", intent_id="in_ap_1", attempt_id="in_ap_1.a1",
                   answer="confirmed", booking_id="B-1", price_inr=1350),
    ]
    states = {}
    for i, (inbound, result, step) in enumerate(zip_longest(inbounds, results, steps)):
        states[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", user_id="u_demo", run_id=f"r_{i:04d}", inbound=inbound,
            errand=errand, system_notes=[SystemNote(kind="slot_taken", text="taken")],
            plan_result=result, result=step, park="ap_1" if i % 2 else None,
            counters=RunCounters(model_calls=3, tool_calls=2, reprompts=1, replans=1,
                                 tokens_in=900, tokens_out=80, cost=Decimal("0.012345"),
                                 started_at=T0, deadline_at=T0 + timedelta(seconds=60)),
            outcome=RunOutcome.PRESENTED_OPTIONS,
        )

    def fill(state: ThreadState):
        full = states[state.thread_id]
        return {name: getattr(full, name) for name in ThreadState.model_fields}

    graph = StateGraph(ThreadState)
    graph.add_node("fill", fill)
    graph.add_edge(START, "fill")
    graph.add_edge("fill", END)

    def same_types(a, b, where):
        assert type(a) is type(b), f"{where}: {type(a).__name__} came back as {type(b).__name__}"
        if isinstance(a, BaseModel):
            for name in type(a).model_fields:
                same_types(getattr(a, name), getattr(b, name), f"{where}.{name}")
        elif isinstance(a, (list, tuple)):
            assert len(a) == len(b), where
            for i, (x, y) in enumerate(zip(a, b)):
                same_types(x, y, f"{where}[{i}]")
        elif isinstance(a, dict):
            assert a.keys() == b.keys(), where
            for k in a:
                same_types(a[k], b[k], f"{where}[{k!r}]")

    async def main(mode, path):
        assert _msgpack.STRICT_MSGPACK_ENABLED
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            app = graph.compile(checkpointer=saver)
            for tid in states:
                cfg = {"configurable": {"thread_id": tid}}
                if mode == "write":
                    seed = {"thread_id": tid, "user_id": "u_demo", "run_id": "r_seed"}
                    await app.ainvoke(seed, cfg, durability="sync", version="v2")
                    continue
                values = (await app.aget_state(cfg)).values
                for name in ThreadState.model_fields:
                    same_types(getattr(states[tid], name), values[name], f"{tid}.{name}")
                assert ThreadState.model_validate(values) == states[tid], tid
        assert not seen, seen
        print(f"{mode} ok: {len(states)} threads")

    asyncio.run(main(sys.argv[1], sys.argv[2]))
    """
)


def run(script: Path, mode: str, db: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error", str(script), mode, str(db)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_full_thread_state_round_trips_through_strict_msgpack_in_a_fresh_process(
    tmp_path: Path,
) -> None:
    script = tmp_path / "roundtrip.py"
    script.write_text(SCRIPT)
    db = tmp_path / "checkpoints.sqlite"
    wrote = run(script, "write", db)
    assert wrote.returncode == 0, wrote.stdout + wrote.stderr
    read = run(script, "read", db)
    assert read.returncode == 0, read.stdout + read.stderr
    assert read.stdout.strip() == "read ok: 13 threads"
    assert read.stderr == "", read.stderr
