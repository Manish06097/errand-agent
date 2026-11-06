"""Suite-wide guards: offline by default, live Claude calls only on explicit opt-in."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest

LIVE_ENV = "ERRAND_LIVE"


def _is_loopback(address: Any) -> bool:
    if isinstance(address, str):  # AF_UNIX path
        return True
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any test that opens a non-loopback connection, unless it is marked live."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded(fn: Any) -> Any:
        def wrapper(self: socket.socket, address: Any) -> Any:
            if not _is_loopback(address):
                raise RuntimeError(f"offline test tried to connect to {address!r}")
            return fn(self, address)

        return wrapper

    monkeypatch.setattr(socket.socket, "connect", guarded(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(real_connect_ex))
    yield


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 to call the real API")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
