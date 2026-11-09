"""The per-worker port helper in tests/conftest.py: no two workers share a port, and no
port lands on the worktree's own three."""

from __future__ import annotations

import pytest

from tests.conftest import MAX_WORKERS, PORT_STRIDE, worker_port

WORKTREES = [
    {},  # defaults: world 8765, api 8000, web 3200
    {"ERRAND_WORLD_PORT": "9150", "ERRAND_API_PORT": "9151", "ERRAND_WEB_PORT": "9152"},
    {"ERRAND_WORLD_PORT": "9090", "ERRAND_API_PORT": "9091", "ERRAND_WEB_PORT": "9092"},
]


def ports_of(base_env: dict[str, str]) -> dict[tuple[int, int], int]:
    ports: dict[tuple[int, int], int] = {}
    for worker in range(MAX_WORKERS):
        env = {**base_env, "PYTEST_XDIST_WORKER": f"gw{worker}"}
        for slot in range(PORT_STRIDE):
            ports[(worker, slot)] = worker_port(slot, env)
    return ports


@pytest.mark.parametrize("env", WORKTREES, ids=["defaults", "9150", "9090"])
def test_no_two_workers_or_slots_share_a_port(env: dict[str, str]) -> None:
    ports = ports_of(env)
    assert len(set(ports.values())) == len(ports)


@pytest.mark.parametrize("env", WORKTREES, ids=["defaults", "9150", "9090"])
def test_no_port_lands_on_the_worktrees_three_base_ports(env: dict[str, str]) -> None:
    base = {
        int(env.get("ERRAND_WORLD_PORT", 8765)),
        int(env.get("ERRAND_API_PORT", 8000)),
        int(env.get("ERRAND_WEB_PORT", 3200)),
    }
    assert not base & set(ports_of(env).values())
    assert max(ports_of(env).values()) < 65536


def test_a_serial_run_counts_as_worker_zero() -> None:
    assert worker_port(0, {"ERRAND_WORLD_PORT": "9150"}) == worker_port(
        0, {"ERRAND_WORLD_PORT": "9150", "PYTEST_XDIST_WORKER": "gw0"}
    )


def test_a_slot_outside_the_block_is_refused() -> None:
    with pytest.raises(ValueError):
        worker_port(PORT_STRIDE, {})
