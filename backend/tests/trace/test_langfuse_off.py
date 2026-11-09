"""With Langfuse off, the app runs a conversation without loading langfuse (spike F1).

This needs a fresh interpreter: the sink tests import langfuse into the pytest process,
so an in-process `sys.modules` check would pass or fail by test order.

What is checked is langfuse itself and OpenTelemetry's export machinery (the SDK's
span processors and the OTLP exporter), not `opentelemetry` as a whole: FastAPI 0.162
imports opentelemetry-api on its own, and langsmith (via langchain-core) imports
`opentelemetry.sdk.trace` whenever the SDK is installed, Langfuse or not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check

BACKEND = Path(__file__).resolve().parents[2]

SCRIPT = """
import asyncio, json, sys
from pathlib import Path

from evals.runner import harness
from tests.trace.langfuse_support import S03_TURNS, s03_world

FORBIDDEN = ("langfuse", "opentelemetry.exporter", "opentelemetry.sdk.trace.export")


async def main(root):
    async with harness(root, s03_world()) as h:
        outcomes = [(await h.say(text)).outcome for text, _ in S03_TURNS]
        health = (await h.client.get("/api/health")).json()
        sink = type(h.manager.trace_sink).__name__
    loaded = sorted(
        m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
    )
    print(json.dumps({"outcomes": outcomes, "health": health, "sink": sink, "loaded": loaded}))


asyncio.run(main(Path(sys.argv[1])))
"""


@pytest.mark.parametrize(
    ("errand_langfuse", "keys", "reason"),
    [(None, False, "no_keys"), ("off", True, "disabled")],
    ids=["keys_absent", "off_with_keys"],
)
def test_a_conversation_with_langfuse_off_never_imports_it(
    tmp_path: Path, errand_langfuse: str | None, keys: bool, reason: str
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    env.pop("ERRAND_LANGFUSE", None)
    if errand_langfuse is not None:
        env["ERRAND_LANGFUSE"] = errand_langfuse
    if keys:
        env.update(
            LANGFUSE_PUBLIC_KEY="pk-test-off",
            LANGFUSE_SECRET_KEY="sk-test-off",
            # Closed loopback port, in case the sink were built anyway.
            LANGFUSE_HOST="http://127.0.0.1:9",
        )
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(BACKEND), env.get("PYTHONPATH")) if p)
    script = tmp_path / "probe.py"
    script.write_text(SCRIPT)
    (tmp_path / "data").mkdir()
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "data")],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["outcomes"] == ["ASKED_USER", "ASKED_USER", "PRESENTED_OPTIONS", "REPLIED"]
    assert report["health"]["langfuse"] == {"state": "off", "reason": reason}
    assert report["sink"] == "NullSink"
    assert report["loaded"] == []
