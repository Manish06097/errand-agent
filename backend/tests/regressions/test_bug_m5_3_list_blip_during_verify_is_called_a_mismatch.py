"""BUG-M5-3: a `list_bookings` failure during verify was reported as a provider mismatch.

verify cross-checks a confirmed booking against `list_bookings` (the provider recorded).
When only that call failed, the booking itself was fine, but the check went into the
mismatch list: the user read "The provider recorded the provider could not be checked
(B-2)" and the status.check carried `mismatch`, the field INV-01 takes as "the agent
caught a booking that differs from the approval". A wrong booking plus a list blip would
then have passed INV-01.

Reproduced end to end: S1-style success, with `list_bookings` failing for the VERIFIER only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errand.tools import gateway
from errand.tools.errors import ToolTransient
from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals import invariants
from evals.runner import harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def world() -> ScenarioConfig:
    return ScenarioConfig(
        id="m5_3", seed=53, world=WorldConfig(book=BookConfig(forced=["success"]))
    )


async def test_a_list_failure_during_verify_is_an_incomplete_check_not_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gateway.ToolGateway.call

    async def call(self: Any, caller: Any, tool: Any, args: Any, **kw: Any) -> Any:
        if caller is gateway.Caller.VERIFIER and tool == "list_bookings":
            raise ToolTransient("list_bookings", 503, b"")
        return await real(self, caller, tool, args, **kw)

    monkeypatch.setattr(gateway.ToolGateway, "call", call)
    async with harness(tmp_path, world()) as h:
        view = await h.say(OPENER)
        assert view.outcome == "AWAITING_APPROVAL"
        convo = await h.conversation()
        card = [m for m in convo["messages"] if m["body"].get("type") == "action"][-1]
        click = await h.decide(card["approval_id"], "approve", card["body"]["fingerprint"])
        view = await h.finish(click.json()["run_id"])
        rec = await h.records()
        convo = await h.conversation()
    assert view.outcome == "PENDING_VERIFICATION"
    last = [m for m in convo["messages"] if m["role"] == "assistant"][-1]["body"]
    assert "could not be checked" not in last["text"] and "provider recorded" not in last["text"]
    assert "couldn't finish checking" in last["text"]
    checks = [r for r in rec.all_records() if r["type"] == "status.check"]
    assert checks and all(not r.get("mismatch") for r in checks)
    assert [i["state"] for i in rec.intents] == ["needs_attention"]
    # Pinned on the same record: every invariant holds, the attempt is applied (the book
    # went through), and the intent says why it is unfinished and which booking it is about.
    assert invariants.check(rec) == {name: [] for name in invariants.DEFAULT}
    assert [a["state"] for a in rec.attempts] == ["applied"]
    result = json.loads(rec.intents[0]["result_json"])
    assert result["reason"] == "check_incomplete" and result["booking_id"]
