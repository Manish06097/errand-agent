"""Makefile entry points behave as documented, run the way a developer runs them."""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check


def make(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(("target", "milestone"), [("e2e", "M7"), ("record", "M7")])
def test_unbuilt_targets_fail_loudly(target: str, milestone: str) -> None:
    result = make(target)
    assert result.returncode == 2
    assert f"not built yet, lands in {milestone}" in result.stderr


def run_evals_wrapper(
    *args: str, evals_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ERRAND_EVALS_DIR", None)
    if evals_dir is not None:
        env["ERRAND_EVALS_DIR"] = str(evals_dir)
    return subprocess.run(
        ["uv", "run", "--frozen", "--project", "backend", "python", "scripts/run_evals.py", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def evals_dir(tmp_path: Path) -> Path:
    """An evals directory of our own, so these tests never run the real suite."""
    dest = tmp_path / "evals_probe"
    dest.mkdir()
    return dest


def test_evals_on_an_empty_directory_fails(evals_dir: Path) -> None:
    result = run_evals_wrapper(evals_dir=evals_dir)
    assert result.returncode == 5, result.stdout + result.stderr
    assert "no scenarios collected" in result.stderr


def test_evals_wrapper_does_not_hide_an_empty_filter() -> None:
    result = run_evals_wrapper("-k", "no_such_scenario")
    assert result.returncode == 5


def test_evals_fails_when_the_only_scenario_file_is_not_collected(evals_dir: Path) -> None:
    # pytest only collects test_*.py, so this scenario would be silently skipped.
    (evals_dir / "booking_timeout_scenario.py").write_text(
        "def test_booking_times_out() -> None:\n    assert True\n"
    )
    result = run_evals_wrapper(evals_dir=evals_dir)
    assert result.returncode != 0, result.stdout + result.stderr


def test_evals_wrapper_runs_a_collected_scenario(evals_dir: Path) -> None:
    (evals_dir / "test_probe.py").write_text("def test_probe() -> None:\n    assert True\n")
    result = run_evals_wrapper(evals_dir=evals_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


PROBE_REPORT_DIR = """
import os
from pathlib import Path

def test_report_dir() -> None:
    seen = os.environ.get("ERRAND_EVAL_REPORT_DIR", "-")
    Path(__file__).with_name("seen.txt").write_text(seen)
"""


@pytest.mark.parametrize("write_report", [False, True])
def test_evals_writes_the_report_only_when_asked(evals_dir: Path, write_report: bool) -> None:
    (evals_dir / "test_report_dir.py").write_text(PROBE_REPORT_DIR)
    args = ("--write-report",) if write_report else ()
    result = run_evals_wrapper(*args, evals_dir=evals_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = str(REPO_ROOT / "docs") if write_report else "-"
    assert (evals_dir / "seen.txt").read_text() == expected


@pytest.fixture
def occupied_port() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


def test_dev_preflight_names_the_taken_port_and_its_variable(occupied_port: int) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env["ERRAND_API_PORT"] = str(occupied_port)
    result = subprocess.run(
        ["uv", "run", "--frozen", "--project", "backend", "python", "scripts/dev.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert f"port {occupied_port} for api is already in use" in result.stderr
    assert "ERRAND_API_PORT" in result.stderr
    # Preflight failed before anything was started.
    assert "starting" not in result.stdout
