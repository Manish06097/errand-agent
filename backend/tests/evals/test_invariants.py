"""Each invariant holds on a real scenario's records and fails on a doctored copy.

The records come from running the scenario for real; each doctoring is the smallest
edit that models the bug the invariant exists to catch. An invariant that cannot be
made to fail proves nothing.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from evals import invariants
from evals.dsl import load_scenario
from evals.invariants import Records
from evals.runner import EVALS, run_scenario


async def records_of(scenario_id: str, root: Path) -> Records:
    captured: list[Records] = []
    root.mkdir()
    scenario = load_scenario(EVALS / "scenarios" / f"{scenario_id}.yaml")
    result = await run_scenario(scenario, root, on_records=captured.append)
    assert result.passed, result.failures
    return captured[0]


@pytest.fixture
async def s02(tmp_path: Path) -> Records:
    return await records_of("S02_only_area_missing", tmp_path / "s02")


@pytest.fixture
async def s67(tmp_path: Path) -> Records:
    return await records_of("S67_area_dodged_twice", tmp_path / "s67")


def fails(name: str, rec: Records) -> list[str]:
    return invariants.INVARIANTS[name](rec)


def test_all_hold_on_the_real_records(s02: Records, s67: Records) -> None:
    for rec in (s02, s67):
        assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}


# ---------------------------------------------------------------- INV-07


def test_inv07_fails_when_a_run_has_no_run_end(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    run_id = doctored.runs[0]["run_id"]
    doctored.traces[run_id] = [r for r in doctored.traces[run_id] if r["type"] != "run.end"]
    assert any("0 run.end" in v for v in fails("INV-07", doctored))


def test_inv07_fails_when_a_run_left_no_message(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    run_id = doctored.runs[1]["run_id"]
    doctored.messages = [m for m in doctored.messages if m["run_id"] != run_id]
    assert any("no non-empty assistant message" in v for v in fails("INV-07", doctored))


def test_inv07_fails_when_the_runs_row_disagrees(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    doctored.runs[0]["status"] = "running"
    assert fails("INV-07", doctored)


# ---------------------------------------------------------------- INV-08


def test_inv08_fails_on_an_illegal_transition(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    transition = next(r for r in doctored.all_records() if r["type"] == "state.transition")
    transition["to"] = "completed"  # gathering -> completed is not in ALLOWED
    assert any("gathering->completed not allowed" in v for v in fails("INV-08", doctored))


def test_inv08_fails_when_a_transition_starts_from_the_wrong_phase(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    transition = next(r for r in doctored.all_records() if r["type"] == "state.transition")
    transition["from"] = "comparing"
    transition["to"] = "quoting"  # allowed pair, but the errand was gathering
    assert any("but the errand was gathering" in v for v in fails("INV-08", doctored))


# ---------------------------------------------------------------- INV-10


def _last_model_call(rec: Records) -> dict[str, object]:
    calls = [r for r in rec.all_records() if r["type"] == "model.call"]
    return calls[-1]


def _review_text(rec: Records) -> str:
    row = next(r for r in rec.op_log if r["op"] == "get_provider")
    return str(json.loads(row["response_json"])["reviews"][0]["text"])


def test_inv10_checked_the_reviews_the_world_served(s02: Records) -> None:
    assert not fails("INV-10", s02)
    assert s02.notes["review_texts_checked"] >= 1


def test_inv10_checks_the_whole_catalogue_when_no_provider_was_opened(s67: Records) -> None:
    # S67 never searches or opens a provider, so nothing was served; the needles come
    # from world.sqlite.
    assert not [r for r in s67.op_log if r["op"] == "get_provider"]
    assert not fails("INV-10", s67)
    assert s67.notes["review_texts_checked"] == len(set(s67.catalog_reviews)) > 0


def test_inv10_fails_on_a_review_the_planner_never_fetched(s67: Records) -> None:
    # A leak through some other path (a digest, memory) of a review no get_provider
    # returned.
    doctored = copy.deepcopy(s67)
    request = _last_model_call(doctored)["request"]
    assert isinstance(request, dict)
    request["messages"][-1]["content"].append(
        {"type": "text", "text": f"Earlier customers said: {s67.catalog_reviews[0]}"}
    )
    assert fails("INV-10", doctored)


def test_inv10_fails_when_review_text_reaches_a_planner_request(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    request = _last_model_call(doctored)["request"]
    assert isinstance(request, dict)
    request["messages"][-1]["content"].append({"type": "text", "text": _review_text(s02)})
    assert fails("INV-10", doctored)


def test_inv10_fails_on_the_sanitized_digest_of_a_review(s02: Records) -> None:
    # What the trace holds if the view put reviews back: the sanitizer's digest.
    doctored = copy.deepcopy(s02)
    digest = hashlib.sha256(_review_text(s02).encode("utf-8")).hexdigest()
    request = _last_model_call(doctored)["request"]
    assert isinstance(request, dict)
    request["messages"][-1]["content"].append(
        {"type": "text", "text": json.dumps({"reviews": [{"text": {"sha256": digest}}]})}
    )
    assert fails("INV-10", doctored)


# ---------------------------------------------------------------- INV-11


def test_inv11_fails_when_a_planner_request_lists_book(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    request = _last_model_call(doctored)["request"]
    assert isinstance(request, dict)
    request["tools"].append({"name": "book", "input_schema": {"type": "object"}})
    assert any("['book']" in v for v in fails("INV-11", doctored))


# ---------------------------------------------------------------- INV-13


def _card(rec: Records, fields: list[str], options: list[str]) -> dict[str, object]:
    first = next(m for m in rec.messages if m["body"].get("type") == "clarify")
    card = copy.deepcopy(first)
    card["body"]["fields"] = fields
    card["body"]["options"] = options
    return card


def test_inv13_fails_on_a_third_card_for_the_same_field(s67: Records) -> None:
    doctored = copy.deepcopy(s67)
    third_run = doctored.runs[2]["run_id"]
    card = _card(doctored, ["area"], ["Indiranagar"])
    card["run_id"] = third_run
    doctored.messages.append(card)
    assert any("asked a third time" in v for v in fails("INV-13", doctored))


def test_inv13_fails_on_a_card_asking_a_filled_field(s02: Records) -> None:
    doctored = copy.deepcopy(s02)
    card = next(m for m in doctored.messages if m["body"].get("type") == "clarify")
    card["body"]["fields"] = ["area", "window"]  # window was filled in the same run
    assert any("window asked although" in v for v in fails("INV-13", doctored))


def test_inv13_fails_on_a_re_ask_without_options(s67: Records) -> None:
    doctored = copy.deepcopy(s67)
    second = [m for m in doctored.messages if m["body"].get("type") == "clarify"][1]
    second["body"]["options"] = []
    assert any("re-asked without options" in v for v in fails("INV-13", doctored))


def test_inv13_does_not_trust_the_guard(s67: Records) -> None:
    # The guard's own rejection record is not evidence either way: drop it and the
    # verdict is unchanged, because INV-13 rebuilds asks from the persisted cards.
    doctored = copy.deepcopy(s67)
    for run_id, records in doctored.traces.items():
        doctored.traces[run_id] = [r for r in records if r["type"] != "guard.ask_rejected"]
    assert fails("INV-13", doctored) == []
