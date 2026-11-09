"""The Python pin check in `make check` can actually fail.

`make check` starts with `uv sync --locked`, which refuses to run when uv.lock no
longer matches the pins in pyproject.toml. `--frozen` would not: it installs the
lock's versions without comparing them to the declared pins. These tests run the
Makefile's own sync line against a temp copy of backend/ with one pin changed.
Everything runs with UV_OFFLINE=1 and --dry-run, so nothing is downloaded or
installed.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # subprocesses or real servers: serial step of make check

BACKEND = REPO_ROOT / "backend"
PIN = '"httpx==0.28.1"'
DRIFTED_PIN = '"httpx==0.28.0"'
SYNC_TARGETS = ("check", "setup", "dev", "evals")


def make_sync_lines() -> list[str]:
    """Every `uv sync` command the Makefile targets would run, as make prints them."""
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", *SYNC_TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [
        line.strip() for line in result.stdout.splitlines() if line.strip().startswith("uv sync")
    ]


def offline_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    env.pop("VIRTUAL_ENV", None)
    env.update(UV_OFFLINE="1", NO_COLOR="1")
    return env


@pytest.fixture
def backend_copy(tmp_path: Path) -> Path:
    """A repo-shaped temp dir holding just what uv needs to judge the lock."""
    dest = tmp_path / "backend"
    dest.mkdir()
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copy2(BACKEND / name, dest / name)
    return tmp_path


def run_sync(line: str, root: Path) -> subprocess.CompletedProcess[str]:
    argv = [*shlex.split(line), "--dry-run"]
    return subprocess.run(
        argv, cwd=root, env=offline_env(), capture_output=True, text=True, timeout=120
    )


def test_every_make_sync_is_locked() -> None:
    lines = make_sync_lines()
    assert len(lines) == len(SYNC_TARGETS), lines
    for line in lines:
        assert re.search(r"(^|\s)--locked(\s|$)", line), line
        assert "--frozen" not in line, line


def test_locked_sync_passes_when_lock_matches(backend_copy: Path) -> None:
    result = run_sync(make_sync_lines()[0], backend_copy)
    assert result.returncode == 0, result.stdout + result.stderr


def test_locked_sync_fails_when_a_pin_drifts(backend_copy: Path) -> None:
    pyproject = backend_copy / "backend" / "pyproject.toml"
    text = pyproject.read_text()
    assert PIN in text, f"{PIN} is the pin this test drifts; update the test if it moved"
    pyproject.write_text(text.replace(PIN, DRIFTED_PIN))

    result = run_sync(make_sync_lines()[0], backend_copy)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "needs to be updated" in result.stderr, result.stderr
