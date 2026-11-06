"""Makefile entry points behave as documented, run the way a developer runs them."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.hygiene.repo import REPO_ROOT


def make(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("target", "milestone"), [("e2e", "M7"), ("record", "M7"), ("langfuse-up", "M15")]
)
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
def evals_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "evals"
    shutil.copytree(REPO_ROOT / "backend" / "evals", dest)
    return dest


def test_evals_with_no_scenarios_reports_zero_and_passes() -> None:
    result = run_evals_wrapper()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("0 scenarios")


def test_evals_wrapper_does_not_hide_an_empty_filter() -> None:
    result = run_evals_wrapper("-k", "no_such_scenario")
    assert result.returncode == 5


def test_evals_copy_with_only_placeholders_still_reports_zero(evals_copy: Path) -> None:
    (evals_copy / "__init__.py").write_text("")
    result = run_evals_wrapper(evals_dir=evals_copy)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("0 scenarios")


def test_evals_wrapper_fails_when_a_scenario_file_is_not_collected(evals_copy: Path) -> None:
    # pytest only collects test_*.py, so this scenario is silently skipped by pytest.
    (evals_copy / "booking_timeout_scenario.py").write_text(
        "def test_booking_times_out() -> None:\n    assert True\n"
    )
    result = run_evals_wrapper(evals_dir=evals_copy)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "0 scenarios" not in result.stdout
    assert "booking_timeout_scenario.py" in result.stderr


def test_evals_wrapper_fails_on_an_uncollected_subdirectory(evals_copy: Path) -> None:
    (evals_copy / "scenarios").mkdir()
    (evals_copy / "scenarios" / "booking.yaml").write_text("name: booking\n")
    result = run_evals_wrapper(evals_dir=evals_copy)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "scenarios/booking.yaml" in result.stderr


def test_evals_wrapper_runs_a_collected_scenario(evals_copy: Path) -> None:
    (evals_copy / "test_probe.py").write_text("def test_probe() -> None:\n    assert True\n")
    result = run_evals_wrapper(evals_dir=evals_copy)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "0 scenarios" not in result.stdout


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
