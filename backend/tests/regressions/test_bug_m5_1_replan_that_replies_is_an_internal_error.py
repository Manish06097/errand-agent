"""BUG-M5-1: a replan whose answer is a plain reply ended the run in INTERNAL_ERROR.

`prepare_proposal` sends the run back to `plan` once when a held quote is over budget
(or a hold fails), leaving a `Replan` in `state.result`. `plan` set only `plan_result`, so
if the planner's second answer was text rather than a new proposal, `respond` found the
stale `Replan` first, had no template for it and said "Something went wrong on my side".
The user was told the run failed when the planner had answered them.

Reproduced end to end: the held quote is over budget, the planner is asked to try again
and answers in words.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage

from errand.llm.scripted import ScriptedRequest
from errand.world.scenario import QuoteDrift, ScenarioConfig, WorldConfig
from evals.policies.common import answer, read
from evals.policies.reference import policy as reference_policy
from evals.runner import harness

OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."


def world() -> ScenarioConfig:
    drift = QuoteDrift(mode="on_hold", providers={f"P{n}": 2000 for n in range(1, 21)})
    return ScenarioConfig(id="m5_1", seed=51, world=WorldConfig(quote_drift=drift))


def replies_after_a_replan(request: ScriptedRequest) -> AIMessage:
    if any("above the budget" in n for n in read(request).notes):
        return answer(request, "None of those fits your budget once the provider holds them.")
    return reference_policy(request)


async def test_a_replan_that_answers_in_words_is_a_reply_not_an_internal_error(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path, world(), policy=replies_after_a_replan) as h:
        view = await h.say(OPENER)
    assert view.outcome == "REPLIED", view.outcome
    text = " ".join(str(m["body"].get("text", "")) for m in view.messages)
    assert "went wrong" not in text and "budget" in text
