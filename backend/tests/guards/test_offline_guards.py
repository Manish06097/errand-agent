"""The suite-wide guards in backend/conftest.py, exercised in a separate pytest process.

Each case copies the real conftest into a fresh pytester directory, so what is tested is
the file every test and eval runs under, not a copy of its logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # subprocesses or real servers: serial step of make check

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


@pytest.fixture
def guarded(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(ROOT_CONFTEST.read_text())
    pytester.makeini("[pytest]\nmarkers =\n    live: real API\n")
    return pytester


def test_a_caught_non_loopback_connect_still_fails_the_test(guarded: pytest.Pytester) -> None:
    guarded.makepyfile(
        """
        import socket

        def test_swallows_the_error():
            try:
                socket.create_connection(("93.184.216.34", 80), timeout=1)
            except Exception:
                pass
        """
    )
    result = guarded.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*used the network*", "*connect to ('93.184.216.34', 80)*"])


def test_the_anthropic_sdk_cannot_hide_a_connection_behind_its_retries(
    guarded: pytest.Pytester,
) -> None:
    guarded.makepyfile(
        """
        import anthropic

        def test_sdk_call_with_a_dummy_key():
            client = anthropic.Anthropic(api_key="dummy-not-a-key", max_retries=1)
            try:
                client.messages.create(
                    model="claude-opus-5-5", max_tokens=8,
                    messages=[{"role": "user", "content": "hi"}],
                )
            except anthropic.APIConnectionError:
                pass  # what the SDK turns our RuntimeError into, after retrying
        """
    )
    result = guarded.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*used the network*", "*resolve 'api.anthropic.com'*"])


def test_loopback_is_allowed_and_live_tests_are_skipped(guarded: pytest.Pytester) -> None:
    guarded.makepyfile(
        """
        import socket
        import pytest

        def test_loopback_round_trip():
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            client = socket.create_connection(server.getsockname(), timeout=1)
            conn, _ = server.accept()
            client.sendall(b"ok")
            assert conn.recv(2) == b"ok"
            for s in (client, conn, server):
                s.close()
            assert socket.getaddrinfo("localhost", 80)

        @pytest.mark.live
        def test_live_only():
            raise AssertionError("must not run without ERRAND_LIVE=1")
        """
    )
    result = guarded.runpytest_subprocess()
    result.assert_outcomes(passed=1, skipped=1)


def test_langfuse_is_off_and_no_api_key_is_reachable(
    guarded: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set in the parent environment, so the inner process starts with them.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-not-real")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-not-real")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3100")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-but-long-enough")
    guarded.makepyfile(
        """
        import os

        def test_environment_is_clean():
            assert os.environ["ERRAND_LANGFUSE"] == "off"
            assert not [k for k in os.environ if k.startswith("LANGFUSE_")]
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert os.environ["ERRAND_ENV_FILE"] == os.devnull
            assert os.environ["ERRAND_MODEL_MODE"] == "scripted"
        """
    )
    result = guarded.runpytest_subprocess()
    result.assert_outcomes(passed=1)
