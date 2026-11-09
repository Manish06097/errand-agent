"""BUG-M3-1: `make evals` passed with no scenarios.

`test_scenarios.py` parametrizes over `scenarios/*.yaml`. When that glob is empty,
pytest does not report "no tests collected" (exit 5, which the wrapper fails on); it
reports one skipped test ("got empty parameter set") and exits 0. So a catalogue whose
YAML was deleted, or renamed to .yml, ran nothing and went green, and the report plugin
quietly left the old table in place. A misnamed file next to real scenarios was simply
never run.

Each test copies the real evals tree, breaks the catalogue the way a person would, and
runs it the way `make evals` does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import report
from evals.dsl import CatalogueError, find_scenarios
from evals.runner import ScenarioResult
from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check

BACKEND = REPO_ROOT / "backend"
REAL_EVALS = BACKEND / "evals"
KEEP = "S67_area_dodged_twice.yaml"  # no world calls, the quickest real scenario


def copy_evals(tmp_path: Path) -> Path:
    dest = tmp_path / "evals"
    shutil.copytree(REAL_EVALS, dest, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return dest


def run_wrapper(evals_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, ERRAND_EVALS_DIR=str(evals_dir))
    return subprocess.run(
        ["uv", "run", "--frozen", "--project", "backend", "python", "scripts/run_evals.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def run_with_report(evals_dir: Path, report_dir: Path) -> subprocess.CompletedProcess[str]:
    # What `make evals` runs with --write-report, pointed at a temp report directory so
    # the committed table is never touched.
    env = dict(os.environ, ERRAND_EVAL_REPORT_DIR=str(report_dir))
    return subprocess.run(
        [
            *("uv", "run", "--frozen", "pytest"),
            *("-c", str(BACKEND / "pyproject.toml"), "--rootdir", str(BACKEND)),
            str(evals_dir),
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_evals_fail_when_the_catalogue_has_no_yaml(tmp_path: Path) -> None:
    evals = copy_evals(tmp_path)
    for path in (evals / "scenarios").glob("*.yaml"):
        path.unlink()
    result = run_wrapper(evals)
    assert result.returncode != 0, result.stdout + result.stderr


def test_evals_fail_when_every_scenario_is_misnamed(tmp_path: Path) -> None:
    evals = copy_evals(tmp_path)
    for path in (evals / "scenarios").glob("*.yaml"):
        path.rename(path.with_suffix(".yml"))
    result = run_wrapper(evals)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "S02_only_area_missing.yml" in result.stdout + result.stderr


def test_evals_fail_when_one_file_next_to_real_scenarios_is_misnamed(tmp_path: Path) -> None:
    evals = copy_evals(tmp_path)
    scenarios = evals / "scenarios"
    for path in scenarios.glob("*.yaml"):
        if path.name != KEEP:
            path.unlink()
    misnamed = scenarios / "S02_only_area_missing.yml"
    shutil.copy(REAL_EVALS / "scenarios" / "S02_only_area_missing.yaml", misnamed)
    result = run_wrapper(evals)
    # S67 still passes; the misnamed S02 must not be silently left out.
    assert result.returncode != 0, result.stdout + result.stderr
    assert misnamed.name in result.stdout + result.stderr


def test_no_table_is_written_for_an_empty_catalogue(tmp_path: Path) -> None:
    evals = copy_evals(tmp_path)
    for path in (evals / "scenarios").glob("*.yaml"):
        path.unlink()
    report_dir = tmp_path / "docs"
    report_dir.mkdir()
    result = run_with_report(evals, report_dir)
    assert result.returncode != 0, result.stdout + result.stderr
    assert sorted(p.name for p in report_dir.iterdir()) == []


def _row(scenario: str) -> ScenarioResult:
    return ScenarioResult(scenario, "c", "scripted:reference", "default", "e", "a", True, "n")


def test_the_report_refuses_a_table_missing_a_collected_scenario() -> None:
    # A scenario that crashed before recording its row must not vanish from the table.
    assert report.refusal([], []) is not None
    assert report.refusal([_row("S02")], ["S02", "S03"]) == "no row for S03"
    assert report.refusal([_row("S02")], ["S02"]) is None


def test_a_subdirectory_of_scenarios_is_refused(tmp_path: Path) -> None:
    (tmp_path / "S02_only_area_missing.yaml").write_text("id: S02_only_area_missing\n")
    (tmp_path / "later").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    with pytest.raises(CatalogueError, match="would never run: later"):
        find_scenarios(tmp_path)
    (tmp_path / "later").rmdir()
    assert [p.name for p in find_scenarios(tmp_path)] == ["S02_only_area_missing.yaml"]
