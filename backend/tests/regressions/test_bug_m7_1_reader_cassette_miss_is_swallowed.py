"""BUG-M7-1: a reader cassette miss was swallowed, so a replay row could pass with no digests.

The planner raises `CassetteMiss` (with the diff against the nearest recording) when a
request has no recording, and the run fails loudly. The quarantined reader's `_call`
caught every exception and turned it into `transport_error`, which fails closed (the
provider simply has no digest). In replay that meant a scenario whose cassette lacked the
reader entries still passed, proving less than its row claimed.

End to end: record S01 with scripted transports into one cassette while the reader times
out (a failed call leaves no entry, so the cassette holds planner entries only, and they
are the requests a digest-less run makes), then replay S01 on it. Every reader request
misses; the row must fail and carry the miss diff.
"""

from __future__ import annotations

import json
from pathlib import Path

from errand.api.app import scripted_factory
from errand.config import ModelSettings
from evals.dsl import ReplaySpec
from evals.policies import reader as reader_doubles
from evals.replay import ReplayRun, find_by_name
from evals.runner import PLANNERS, Runner, harness, run_scenario

READER_MODEL = "claude-haiku-5-5"


async def record_s01(root: Path, cassette: Path) -> None:
    scenario = find_by_name("S01")
    policy = PLANNERS[scenario.model]
    reader = reader_doubles.READERS["timeout"]
    settings = ModelSettings(model_mode="record", cassette_path=cassette)
    async with harness(
        root,
        scenario.world_config(),
        policy,
        reader=reader,
        model_factory=scripted_factory(policy),
        reader_factory=scripted_factory(reader),
        model_settings=settings,
    ) as h:
        runner = Runner(scenario, h, policy)
        await runner.run()
        assert runner.failures == []


async def test_a_replay_row_without_the_reader_entries_fails_with_the_miss_diff(
    tmp_path: Path,
) -> None:
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    await record_s01(tmp_path / "record", cassette_dir / "S01.jsonl")
    recorded = [
        json.loads(line)["request"].get("model")
        for line in (cassette_dir / "S01.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert recorded and READER_MODEL not in recorded, recorded

    run = ReplayRun(ReplaySpec(cassette="S01"), directory=cassette_dir)
    result = await run_scenario(find_by_name("S01"), tmp_path / "replay", replay=run)

    assert not result.passed, (
        "the replay row passed although the reader's requests have no recording"
    )
    text = "\n".join(result.failures)
    assert "cassette miss" in text and "nearest recording" in text, text
