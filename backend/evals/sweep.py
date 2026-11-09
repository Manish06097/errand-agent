"""SWEEP (M5): the reference policy over seeds 1 to 200 of the default world.

The default world is the brief's distribution: book succeeds 70%, the slot is taken 15%,
times out after committing 7.5% and times out without booking 7.5%. One seed is one whole
conversation: the user asks for a plumber, approves the card, and then does what a user
would: after a `slot_taken` replan they pick again and approve again; the driver stops on
BOOKED_VERIFIED (a booking confirmed by the provider) or PENDING_VERIFICATION (about 0.56%
of seeds: a second `timeout_not_booked` on the same-key retry spends the budget). Every
invariant must hold on every seed, whatever path the draws took.

Each seed is its own test (`SWEEP[seed=N]`), so a failure names its seed; the report plugin
folds the items into the one table row `n_pass/200`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from errand.domain.brief import BriefField
from errand.world.scenario import ScenarioConfig
from evals import invariants
from evals.dsl import Approve, Say, Scenario, Until, UserSpec
from evals.policies import reference
from evals.runner import Runner, harness

SEEDS = range(1, 201)
OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500."
STOPS = ("BOOKED_VERIFIED", "PENDING_VERIFICATION")
MAX_ROUNDS = 5


@dataclass
class SweepResult:
    seed: int
    outcome: str | None
    failures: list[str] = field(default_factory=list)
    rounds: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures


def _scenario(seed: int) -> Scenario:
    return Scenario(
        id=f"SWEEP_{seed}",
        title="sweep",
        category=["booking_correctness"],
        seed=seed,
        user=UserSpec(
            persona="cooperative",
            goal={
                BriefField.SERVICE: "plumber",
                BriefField.AREA: "Indiranagar",
                BriefField.WINDOW: "this week mornings",
                BriefField.BUDGET: "1500",
            },
        ),
        steps=[],
    )


async def run_seed(seed: int, root: Path) -> SweepResult:
    scenario = _scenario(seed)
    result = SweepResult(seed=seed, outcome=None)
    async with harness(root, ScenarioConfig(id=scenario.id, seed=seed), reference.policy) as h:
        runner = Runner(scenario, h, reference.policy, "on")
        try:
            await runner.step(
                Say(
                    say=OPENER,
                    gives=[
                        BriefField.SERVICE,
                        BriefField.ISSUE,
                        BriefField.AREA,
                        BriefField.WINDOW,
                        BriefField.BUDGET,
                    ],
                )
            )
            await runner.step(Until(until="approval_card"))
            for result.rounds in range(1, MAX_ROUNDS + 1):
                await runner.step(Approve())
                result.outcome = runner.last.outcome
                if result.outcome in STOPS:
                    break
                if result.outcome != "PRESENTED_OPTIONS":
                    result.failures.append(f"round {result.rounds}: outcome {result.outcome}")
                    break
                # The booking was refused and the planner offered other slots: the user picks.
                await runner.step(Until(until="approval_card"))
            else:
                result.failures.append(f"no verified or pending end after {MAX_ROUNDS} rounds")
        except AssertionError as exc:
            result.failures.append(str(exc))
        records = await h.records()
    result.failures += runner.failures
    violations = invariants.check(records, ["default"])
    result.failures += [f"{name}: {v}" for name, vs in violations.items() for v in vs]
    return result
