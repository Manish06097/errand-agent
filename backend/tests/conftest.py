"""Shared test helpers. Fixtures that every suite needs live in backend/conftest.py."""

from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

from evals.runner import TRACE_DIR_ENV

DEFAULT_WORLD_PORT = 8765
# Each xdist worker owns PORT_STRIDE consecutive ports, starting PORT_BLOCK_OFFSET above
# the worktree's world port.
PORT_BLOCK_OFFSET = 1000
PORT_STRIDE = 8
MAX_WORKERS = 64


def worker_index(env: Mapping[str, str] | None = None) -> int:
    """0 for a serial run, n for xdist's `gwN`."""
    name = (os.environ if env is None else env).get("PYTEST_XDIST_WORKER", "")
    if not name:
        return 0
    if not (name.startswith("gw") and name[2:].isdigit()):
        raise RuntimeError(f"unrecognised PYTEST_XDIST_WORKER {name!r}")
    return int(name[2:])


def worker_port(slot: int = 0, env: Mapping[str, str] | None = None) -> int:
    """A fixed port for a test that cannot bind port 0 (a server the test starts by a
    command line that takes a port). Anything that can bind port 0 should keep doing so.

        port = ERRAND_WORLD_PORT (default 8765) + 1000 + 8 * worker + slot

    `worker` is n for xdist's `gwN` and 0 for a serial run; `slot` is 0..7, for a test
    that needs several ports at once. The block starts 1000 above the world port, so it
    clears that port and the API and web ports, which sit next to it or at their defaults
    (8000, 3200) below it. Two workers never share a port: their blocks are 8 apart and
    a slot is below 8. Up to 64 workers fit.
    """
    values = os.environ if env is None else env
    if not 0 <= slot < PORT_STRIDE:
        raise ValueError(f"slot must be 0..{PORT_STRIDE - 1}, got {slot}")
    worker = worker_index(values)
    if worker >= MAX_WORKERS:
        raise RuntimeError(f"at most {MAX_WORKERS} workers are supported, got gw{worker}")
    base = int(values.get("ERRAND_WORLD_PORT", DEFAULT_WORLD_PORT))
    return base + PORT_BLOCK_OFFSET + PORT_STRIDE * worker + slot


@pytest.fixture(autouse=True)
def _private_eval_trace_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that runs a scenario saves its traces under its own temp directory, not
    under backend/evals/out, which parallel workers would write at once."""
    monkeypatch.setenv(TRACE_DIR_ENV, str(tmp_path_factory.mktemp("eval-traces")))
