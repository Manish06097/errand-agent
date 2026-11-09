"""BUG-N2B-1: INV-19 failed on the 60-row seed: a forgotten `access_note` whose evidence
message was redacted under a newer row's id.

Forgetting a key forgets its superseded ancestors too, in one chain. The redaction rows of
the whole chain were all attributed to `chain[0]`, the newest row, so the older row (m-50)
had its evidence message overwritten but no `message_redactions` row of its own. INV-19 asks
for one per forgotten row with user-message evidence, and a later "restore the history of
this row" or the trace reader's per-row accounting could not find it.

The end-to-end repro is the scenario S64_rich: it loads `memory_rich_v1`, asks "what do you
remember about me?" and checks every invariant over the seeded database.
"""

from __future__ import annotations

from pathlib import Path

from evals.dsl import load_scenario
from evals.runner import EVALS, run_scenario


async def test_every_forgotten_row_has_a_redaction_of_its_own(tmp_path: Path) -> None:
    scenario = load_scenario(EVALS / "scenarios" / "S64_rich_what_do_you_remember.yaml")
    result = await run_scenario(scenario, tmp_path)
    inv19 = [f for f in result.failures if "INV-19" in f]
    assert inv19 == [], inv19
