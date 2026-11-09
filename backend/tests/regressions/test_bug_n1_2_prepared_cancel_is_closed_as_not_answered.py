"""BUG-N1-2: a cancel left `prepared` by a crash was closed "the provider didn't answer".

Crash at `after_intent_write` on a cancel and restart: the attempt was written and never
sent, zero cancel calls reached the provider, yet verify closed it `not_applied` and the
user read "I couldn't cancel it: the provider didn't answer", a sentence the records do
not support, with no way to retry. A `prepared` attempt is provably unsent, so it is sent
once with its own key like a booking (9.4 step 6, S36), the token coming back from the
consumed approval (`token_for_consumed`), and then verified through `list_bookings`.

The end-to-end proof is S36b (a real process kill and restart); S36c is the reschedule twin.
"""

from __future__ import annotations

from pathlib import Path

from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario


async def test_a_cancel_prepared_before_the_crash_is_sent_once_after_the_restart(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S36b_cancel_crash_after_intent_write.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures


async def test_a_reschedule_prepared_before_the_crash_is_sent_once_after_the_restart(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S36c_reschedule_crash_after_intent_write.yaml")
    result = await run_scenario(scenario, tmp_path)
    assert result.passed, result.failures
