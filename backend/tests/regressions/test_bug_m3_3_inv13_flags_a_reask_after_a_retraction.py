"""BUG-M3-3: INV-13 flagged a legal re-ask after the user took a value back.

`update_brief` accepts `clear` in M3, so a user can retract a value: "need a plumber"
asks area, window and budget; "indiranagar" fills the area; "not indiranagar actually,
this week mornings, 1500" clears it and fills the rest. The area is missing again, so
the ask guard accepts its one re-ask with options. INV-13 rebuilt "filled since the
first ask" from the brief.patch records but never undid it on a clear, and reported
"area re-asked after the user's reply filled it" on a run the guard got right.

The ruling: a retraction is not the user giving the value. After an applied clear the
field is unfilled; a re-ask with options is legitimate and still counts toward the
two-asks limit.

Both flows run end to end through the API with a planner that clears on a retraction.
The second one takes the clear while COMPARING, so COMPARING -> GATHERING is exercised.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals import invariants
from evals.invariants import Records
from evals.policies import correction
from evals.runner import Harness, expected_search_args, harness
from tests.api.support import OPENER, world


def records_of_type(rec: Records, run_id: str, kind: str) -> list[dict[str, object]]:
    return [r for r in rec.traces[run_id] if r["type"] == kind]


async def searches(h: Harness) -> list[dict[str, object]]:
    return [json.loads(r["args_json"]) for r in await h.op_log() if r["op"] == "search_providers"]


@pytest.fixture
async def gathering(tmp_path: Path) -> Records:
    """The lead's flow: the retraction arrives while the errand is still GATHERING."""
    async with harness(tmp_path, world("bug_m3_3_gathering"), policy=correction.policy) as h:
        first = await h.say("need a plumber")
        assert first.outcome == "ASKED_USER"
        card = first.card()
        assert card is not None
        assert [f.value for f in card.fields] == ["area", "window", "budget_max_inr"]

        second = await h.say("indiranagar")
        assert second.outcome == "ASKED_USER"
        card = second.card()
        assert card is not None and card.options
        assert [f.value for f in card.fields] == ["window", "budget_max_inr"]

        third = await h.say("not indiranagar actually, this week mornings, 1500")
        assert third.outcome == "ASKED_USER"
        card = third.card()
        assert card is not None
        assert [f.value for f in card.fields] == ["area"]
        assert card.options, "a re-ask carries quick replies"

        state = (await h.conversation())["state"]
        assert state["errand"]["brief"]["area"] is None
        assert state["errand"]["phase"] == "gathering"
        assert await searches(h) == []
        rec = await h.records()

    patches = records_of_type(rec, third.run_id, "brief.patch")
    assert [p["fields"] for p in patches] == [
        {"set": ["window", "budget_max_inr"], "clear": ["area"]}
    ]
    assert records_of_type(rec, third.run_id, "guard.ask_rejected") == []
    return rec


async def test_a_re_ask_after_a_retraction_while_gathering_holds_inv13(gathering: Records) -> None:
    assert invariants.check(gathering) == {name: [] for name in invariants.DEFAULT}


async def test_a_retraction_while_comparing_goes_back_to_gathering(tmp_path: Path) -> None:
    async with harness(tmp_path, world("bug_m3_3_comparing"), policy=correction.policy) as h:
        first = await h.say(OPENER)
        assert first.outcome == "ASKED_USER"
        card = first.card()
        assert card is not None and [f.value for f in card.fields] == ["area"]

        second = await h.say("indiranagar")
        assert second.outcome == "PRESENTED_OPTIONS"
        assert (await h.conversation())["state"]["errand"]["phase"] == "comparing"

        third = await h.say("not indiranagar actually")
        assert third.outcome == "ASKED_USER"
        card = third.card()
        assert card is not None and [f.value for f in card.fields] == ["area"]
        assert card.options
        assert (await h.conversation())["state"]["errand"]["phase"] == "gathering"

        fourth = await h.say("koramangala")
        assert fourth.outcome == "PRESENTED_OPTIONS"
        state = (await h.conversation())["state"]
        assert state["errand"]["phase"] == "comparing"
        done = await searches(h)
        assert [s["area"] for s in done] == ["indiranagar", "koramangala"]
        assert done[-1] == expected_search_args(state)
        rec = await h.records()

    moves = [
        (t["from"], t["to"], t["reason"])
        for t in records_of_type(rec, third.run_id, "state.transition")
    ]
    assert ("comparing", "gathering", "required_field_cleared") in moves
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


# ------------------------------------------------- INV-13 still fails without a clear


def _drop_area_clear(rec: Records, *, keep_record: bool) -> Records:
    """The third run's patch, as if the user had not taken the area back."""
    doctored = copy.deepcopy(rec)
    third = doctored.runs[2]["run_id"]
    for record in doctored.traces[third]:
        if record["type"] == "brief.patch" and "area" in record["fields"]["clear"]:
            if keep_record:
                record["evidence_ok"] = False  # a rejected patch changes nothing
            else:
                record["fields"]["clear"] = []
    return doctored


def test_inv13_fails_on_a_re_ask_of_a_field_filled_and_never_cleared(gathering: Records) -> None:
    doctored = _drop_area_clear(gathering, keep_record=False)
    found = invariants.INVARIANTS["INV-13"](doctored)
    assert any("area asked although the user already gave it" in v for v in found), found
    assert any("area re-asked after the user's reply filled it" in v for v in found), found


def test_inv13_ignores_a_clear_that_was_rejected(gathering: Records) -> None:
    doctored = _drop_area_clear(gathering, keep_record=True)
    found = invariants.INVARIANTS["INV-13"](doctored)
    assert any("area re-asked after the user's reply filled it" in v for v in found), found
