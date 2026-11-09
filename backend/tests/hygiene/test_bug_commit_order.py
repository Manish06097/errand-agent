"""The bugs write-up promises, for each bug it tells, that the failing repro commit comes
before the fix commit in history. This pins that down with git, so history surgery
cannot quietly break it: every repro SHA exists, is an ancestor of the fix SHA it pairs
with, and both are reachable from HEAD. The pairs are also checked against the write-up
and the log, so the three documents cannot drift apart."""

from __future__ import annotations

import subprocess

import pytest

from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # shells out to git

# (bug id, repro SHA, fix SHA): round 1 of BUG-M4-2, then its last round.
PAIRS = [
    ("BUG-M1-3", "87f1329", "18a9719"),
    ("BUG-M3-1", "cd29408", "8ced0c1"),
    ("BUG-M4-2", "6fdc8e0", "5bcfda3"),
    ("BUG-M4-2", "b16e6db", "d4bc3ed"),
]


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


@pytest.mark.parametrize(("bug", "repro", "fix"), PAIRS)
def test_the_repro_commit_precedes_the_fix_commit(bug: str, repro: str, fix: str) -> None:
    for sha in (repro, fix):
        assert git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0, f"{bug}: no {sha}"
        assert git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0, (
            f"{bug}: {sha} is not reachable from HEAD"
        )
    assert git("merge-base", "--is-ancestor", repro, fix).returncode == 0, (
        f"{bug}: repro {repro} is not an ancestor of fix {fix}"
    )
    assert repro != fix
    assert "failing repro" in git("log", "-1", "--format=%s", repro).stdout.lower(), (
        f"{bug}: {repro} is not a commit that says 'failing repro'"
    )


def test_the_write_up_and_the_log_name_the_same_shas() -> None:
    write_up = (REPO_ROOT / "docs" / "BUGS_WRITEUP.md").read_text(encoding="utf-8")
    log = (REPO_ROOT / "docs" / "BUGS.md").read_text(encoding="utf-8")
    for bug, repro, fix in PAIRS:
        row = next(line for line in log.splitlines() if line.startswith(f"| {bug} |"))
        for sha in (repro, fix):
            assert sha in row, f"{bug}: {sha} is not in its BUGS.md row"
            assert sha in write_up, f"{bug}: {sha} is not in docs/BUGS_WRITEUP.md"
