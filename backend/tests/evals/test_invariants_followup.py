"""INV-17 (every clause), the watch-run side of INV-07, INV-01 with a Rebook and INV-15 with
a proactive line hold on real records and fail on doctored ones."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals import invariants
from evals.invariants import Records
from tests.evals.test_invariants import records_of


@pytest.fixture
async def s39(tmp_path: Path) -> Records:
    return await records_of("S39_provider_cancels_and_user_rebooks", tmp_path / "s39")


@pytest.fixture
async def s39p(tmp_path: Path) -> Records:
    return await records_of("S39_typed_grant_past_a_proactive_line", tmp_path / "s39p")


def inv17(rec: Records) -> list[str]:
    return invariants.INVARIANTS["INV-17"](rec)


def followup_run(rec: Records) -> str:
    conv = next(c["conversation_id"] for c in rec.conversations if c["kind"] == "followup")
    return next(r["run_id"] for r in rec.runs if r["conversation_id"] == conv)


def test_the_followup_invariants_hold_on_real_records(s39: Records, s39p: Records) -> None:
    for rec in (s39, s39p):
        checked = invariants.check(rec)
        assert all(not v for v in checked.values()), checked
    assert s39.followup_jobs and s39.notifications


def test_inv17_is_in_the_default_set() -> None:
    assert "INV-17" in invariants.DEFAULT


def test_inv17_fails_on_a_job_left_detected(s39: Records) -> None:
    s39.followup_jobs[0]["state"] = "detected"
    assert any("ended detected" in v for v in inv17(s39))


def test_inv17_fails_on_a_second_notification_for_a_job(s39: Records) -> None:
    s39.notifications.append(copy.deepcopy(s39.notifications[0]) | {"notification_id": "n_dup"})
    assert any("2 notifications for job" in v for v in inv17(s39))


def test_inv17_fails_on_a_model_call_before_the_users_first_message(s39: Records) -> None:
    run = followup_run(s39)
    start = next(r for r in s39.traces[run] if r["type"] == "run.start")
    s39.traces[run].append({**start, "type": "model.call", "seq": 999})
    assert any("model.call in follow-up conversation" in v for v in inv17(s39))


def test_inv17_fails_on_a_reader_call_before_the_users_first_message(s39: Records) -> None:
    run = followup_run(s39)
    start = next(r for r in s39.traces[run] if r["type"] == "run.start")
    s39.traces[run].append({**start, "type": "reader.call", "seq": 999})
    assert any("reader.call in follow-up conversation" in v for v in inv17(s39))


def test_inv17_fails_on_a_book_without_a_user_grant(s39: Records) -> None:
    conv = next(c["conversation_id"] for c in s39.conversations if c["kind"] == "followup")
    approval = next(a for a in s39.approvals if a["conversation_id"] == conv)
    approval["granted_via"] = None
    assert any("not granted by the user" in v for v in inv17(s39))


def test_inv17_fails_on_a_grant_that_predates_the_follow_up(s39: Records) -> None:
    conv = next(c for c in s39.conversations if c["kind"] == "followup")
    approval = next(a for a in s39.approvals if a["conversation_id"] == conv["conversation_id"])
    approval["granted_at"] = conv["created_at"] - 1
    assert any("before the follow-up conversation existed" in v for v in inv17(s39))


def test_inv17_fails_on_a_book_with_the_original_chats_card(s39: Records) -> None:
    conv = next(c["conversation_id"] for c in s39.conversations if c["kind"] == "followup")
    intent = next(i for i in s39.intents if i["conversation_id"] == conv)
    original = next(a for a in s39.approvals if a["conversation_id"] != conv)
    intent["approval_id"] = original["approval_id"]
    assert any("no approval card in" in v for v in inv17(s39))


# ---------------------------------------------------------------- INV-07 and watch runs


def watch_run(rec: Records) -> str:
    return next(
        run_id
        for run_id, records in rec.traces.items()
        if any(r["type"] == "run.start" and r.get("kind") == "watch" for r in records)
    )


def test_inv07_accepts_a_watch_run_with_no_outcome_and_no_runs_row(s39: Records) -> None:
    run = watch_run(s39)
    end = next(r for r in s39.traces[run] if r["type"] == "run.end")
    assert end.get("outcome") is None and run not in {r["run_id"] for r in s39.runs}
    assert invariants.inv07_one_end_per_run(s39) == []


def test_inv07_fails_on_a_watch_run_without_its_end(s39: Records) -> None:
    run = watch_run(s39)
    s39.traces[run] = [r for r in s39.traces[run] if r["type"] != "run.end"]
    assert any(
        "watch run has 1 run.start and 0 run.end" in v
        for v in invariants.inv07_one_end_per_run(s39)
    )


def test_inv07_fails_on_a_watch_run_with_an_outcome(s39: Records) -> None:
    run = watch_run(s39)
    next(r for r in s39.traces[run] if r["type"] == "run.end")["outcome"] = "REPLIED"
    assert any("watch run has an outcome" in v for v in invariants.inv07_one_end_per_run(s39))


def test_inv07_fails_on_a_trace_with_no_runs_row_that_is_not_a_watch(s39: Records) -> None:
    run = watch_run(s39)
    for r in s39.traces[run]:
        r.pop("kind", None)
    assert any("a trace with no runs row" in v for v in invariants.inv07_one_end_per_run(s39))


# ---------------------------------------------------------------- INV-01 with a Rebook


def test_inv01_reads_replaces_booking_id_from_the_approval_not_the_world(s39: Records) -> None:
    assert invariants.inv01_side_effects_match_a_consumed_approval(s39) == []
    conv = next(c["conversation_id"] for c in s39.conversations if c["kind"] == "followup")
    approval = next(a for a in s39.approvals if a["conversation_id"] == conv)
    material = json.loads(approval["material_json"])
    assert material["replaces_booking_id"]
    # Doctored: the material loses the field, so the fingerprint no longer matches what was
    # rebuilt from the world plus the approval's own material.
    del material["replaces_booking_id"]
    approval["material_json"] = json.dumps(material)
    assert any(
        "is not the one rebuilt" in v
        for v in invariants.inv01_side_effects_match_a_consumed_approval(s39)
    )


# ---------------------------------------------------------------- INV-15 with a proactive line


def test_inv15_looks_past_a_proactive_line(s39p: Records) -> None:
    proactive = [m for m in s39p.messages if m["kind"] == "proactive"]
    assert proactive, "the scenario writes a proactive line below a live card"
    assert invariants.inv15_parked_on_every_live_card(s39p) == []
    # Doctored: the same line as an ordinary notice below the card is not looked past.
    for m in proactive:
        m["kind"] = "notice"
    assert invariants.inv15_parked_on_every_live_card(s39p) != []
