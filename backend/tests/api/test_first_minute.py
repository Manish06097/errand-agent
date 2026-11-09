"""What a user hits in the first minute, end to end through the API (M8 review round 1).

Each case sends one ordinary message to the reference planner and checks that the agent
did the work instead of asking for it all again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from errand.world.scenario import BookConfig, ScenarioConfig, WorldConfig
from evals.runner import harness


def world() -> ScenarioConfig:
    return ScenarioConfig(
        id="m8-first-minute", seed=8, world=WorldConfig(book=BookConfig(forced=["success"]))
    )


@pytest.mark.parametrize(
    "budget",
    ["under Rs.1500", "under Rs1500", "under INR1500", "under 1500rs", "under 1,500rs"],
)
async def test_a_rupee_budget_in_any_common_spelling_fills_the_brief_and_searches(
    tmp_path: Path, budget: str
) -> None:
    text = f"Find a plumber in Indiranagar for my kitchen tap this week, mornings only, {budget}."
    async with harness(tmp_path, world()) as h:
        view = await h.say(text)
        rec = await h.records()
        convo = await h.conversation()
        searches = [r for r in await h.op_log() if r["op"] == "search_providers"]
    kinds = [r["type"] for r in rec.traces[view.run_id]]
    assert "guard.evidence_rejected" not in kinds
    assert view.outcome == "PRESENTED_OPTIONS"
    assert convo["state"]["errand"]["brief"]["budget_max_inr"]["value"] == 1500
    assert len(searches) == 1
