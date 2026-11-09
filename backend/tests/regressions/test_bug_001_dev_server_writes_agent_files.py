"""BUG-001: `make dev` wrote an AGENTS.md into web/ when run under a coding agent.

Next 16.4's dev server detects AI coding agents (through @vercel/detect-agent, which
honours AI_AGENT) and writes a managed AGENTS.md at the project root. The generated
text contains U+2014 and tells agents to commit it, so after one `make dev` the tree
had an untracked web/AGENTS.md that the em-dash guard rejects and that would put
AI-tool content in the repo. The repro runs the real `make dev` supervisor on
ports, loads the page through the web server, stops it with Ctrl-C (SIGINT) and
checks web/ for agent files.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import worker_port
from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check

WEB = REPO_ROOT / "web"
AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
READY_TIMEOUT_S = 90.0


def wait_for(url: str, deadline: float, dev: subprocess.Popen[str]) -> str | None:
    """None once `url` answers 200, else why it never did."""
    while time.monotonic() < deadline:
        if dev.poll() is not None:
            return f"make dev exited with code {dev.returncode} before {url} answered"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return None
        except OSError:
            time.sleep(0.5)
    return f"{url} not ready in {READY_TIMEOUT_S:.0f}s"


def test_make_dev_under_a_coding_agent_writes_no_agent_files(tmp_path: Path) -> None:
    present_before = [name for name in AGENT_FILES if (WEB / name).exists()]
    if present_before:
        pytest.fail(f"remove {present_before} from web/ first; the repro needs a clean tree")

    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env.update(
        AI_AGENT="errand-regression-test",
        ERRAND_WORLD_PORT=str(worker_port(0)),
        ERRAND_API_PORT=str(worker_port(1)),
        ERRAND_WEB_PORT=str(worker_port(2)),
        # Keep the world's store and the API's files out of the repo's data/ directory.
        ERRAND_WORLD_DB=str(tmp_path / "world.sqlite"),
        ERRAND_DATA_DIR=str(tmp_path / "agent-data"),
    )
    dev = subprocess.Popen(
        ["uv", "run", "--frozen", "--project", "backend", "python", "scripts/dev.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    problem: str | None = None
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        web = f"http://127.0.0.1:{env['ERRAND_WEB_PORT']}"
        problem = wait_for(f"{web}/", deadline, dev) or wait_for(f"{web}/api/health", deadline, dev)
        if problem is None:
            # Next writes the file shortly after startup; give it the window a user would.
            time.sleep(3)
    finally:
        if dev.poll() is None:
            dev.send_signal(signal.SIGINT)
        output, _ = dev.communicate(timeout=30)
    assert problem is None, f"{problem}\n{output[-3000:]}"

    created = [name for name in AGENT_FILES if (WEB / name).exists()]
    for name in created:
        Path(WEB / name).unlink()  # leave the tree as we found it
    assert not created, f"make dev wrote {created} into web/\n{output[-3000:]}"
