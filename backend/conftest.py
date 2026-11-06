"""Suite-wide guards: offline by default, live Claude calls only on explicit opt-in.

This file sits at the pytest rootdir (backend/), an ancestor of both tests/ and
evals/, so every test and every eval scenario runs under the same guards, and
scripts/run_evals.py does not see it as an eval file.

- Socket guard: any non-loopback connect, connect_ex, sendto or name lookup is
  recorded. It also raises, but code under test may catch that (the Anthropic SDK
  wraps it in APIConnectionError and retries), so the test is failed from the
  recorded violations after its call phase, whatever the code did with the error.
- Langfuse off: ERRAND_LANGFUSE=off and every LANGFUSE_* variable removed.
- No credentials: outside live tests ANTHROPIC_API_KEY is removed and the .env file
  is pointed at /dev/null, so no offline test can reach the real key.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Generator, Iterator
from typing import Any

import pytest

pytest_plugins = ("pytester",)

LIVE_ENV = "ERRAND_LIVE"
_VIOLATIONS = pytest.StashKey[list[str]]()
_REPORTED = pytest.StashKey[int]()


def _is_loopback_host(host: Any) -> bool:
    if host is None:
        return True  # getaddrinfo(None, port) resolves the local wildcard address
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_loopback(address: Any) -> bool:
    if isinstance(address, str | bytes):  # AF_UNIX path
        return True
    host = address[0] if isinstance(address, tuple) else address
    return _is_loopback_host(host)


def _is_ip_literal(host: Any) -> bool:
    try:
        ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return True


def _is_live(item: pytest.Item) -> bool:
    return item.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Record and refuse any non-loopback network use, unless the test is marked live."""
    if _is_live(request.node):
        yield
        return
    violations: list[str] = []
    request.node.stash[_VIOLATIONS] = violations

    def refuse(what: str) -> RuntimeError:
        violations.append(what)
        return RuntimeError(f"offline test tried to {what}")

    def guarded_connect(fn: Any) -> Any:
        def wrapper(self: socket.socket, address: Any) -> Any:
            if not _is_loopback(address):
                raise refuse(f"connect to {address!r}")
            return fn(self, address)

        return wrapper

    real_sendto = socket.socket.sendto

    def sendto(self: socket.socket, data: Any, *args: Any) -> Any:
        # sendto(data, address) or sendto(data, flags, address)
        if args and not _is_loopback(args[-1]):
            raise refuse(f"sendto {args[-1]!r}")
        return real_sendto(self, data, *args)

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        # A DNS lookup already leaves the machine, so it counts even if no connect follows.
        if not _is_loopback_host(host) and not _is_ip_literal(host):
            raise refuse(f"resolve {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect(socket.socket.connect_ex))
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    if len(violations) > request.node.stash.get(_REPORTED, 0):
        # Violations during setup or teardown, which the call report did not cover.
        pytest.fail("non-loopback network use in an offline test:\n  " + "\n  ".join(violations))


@pytest.fixture(autouse=True)
def _no_external_services(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Langfuse forced off; outside live tests, no API key from the env or .env."""
    monkeypatch.setenv("ERRAND_LANGFUSE", "off")
    for name in list(os.environ):
        if name.startswith("LANGFUSE_"):
            monkeypatch.delenv(name)
    if not _is_live(request.node):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ERRAND_ENV_FILE", os.devnull)
        monkeypatch.setenv("ERRAND_MODEL_MODE", "scripted")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Any, None]:
    outcome = yield
    report = outcome.get_result()
    violations = item.stash.get(_VIOLATIONS, None)
    if report.when != "call" or not violations:
        return
    item.stash[_REPORTED] = len(violations)
    if report.passed:
        report.outcome = "failed"
        report.longrepr = (
            "the test passed, but it used the network (offline tests must not, even if the "
            "error is caught):\n  " + "\n  ".join(violations)
        )
    else:
        report.sections.append(("network violations", "\n".join(violations)))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 to call the real API")
    for item in items:
        if _is_live(item):
            item.add_marker(skip)
