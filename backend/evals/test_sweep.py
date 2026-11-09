"""SWEEP: one test per seed, so a failure names its seed (see evals/sweep.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import report
from evals.sweep import SEEDS, run_seed


@pytest.mark.parametrize("seed", SEEDS, ids=[f"SWEEP[seed={s}]" for s in SEEDS])
async def test_sweep(seed: int, tmp_path: Path) -> None:
    result = await run_seed(seed, tmp_path)
    report.record_sweep(seed, result.passed, result.outcome, result.rounds)
    assert result.passed, f"seed {seed}: " + "\n".join(result.failures)
