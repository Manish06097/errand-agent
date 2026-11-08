"""BUG-M15-1: `make langfuse-up` hung forever when the Docker daemon stopped answering.

Found by the lead in review, with the daemon on this machine hung: `docker info` never
returns, and `make langfuse-up` (and `make langfuse-down`) waited on it with no timeout
instead of failing clearly. Reproduced end to end through `make langfuse-up` against
that real hung daemon (killed after 45 s), then here with a fake `docker` that hangs on
one subcommand. Each target must fail with exit 2 and say the daemon is not answering,
well within its timeout.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.hygiene.repo import REPO_ROOT

# The stack script's docker timeout, shortened so the test stays quick.
TIMEOUT_S = 2
# Far longer than TIMEOUT_S, far shorter than the fake's hang.
GIVE_UP_S = 25


def fake_docker(tmp_path: Path, hang: str) -> Path:
    """A docker that never answers `hang`, reports 8 GiB to `info`, and fails the rest."""
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        # exec, so the timeout kills the sleeper itself and no child keeps the pipe open.
        f'if [ "$1" = "{hang}" ]; then exec sleep 120; fi\n'
        'if [ "$1" = "info" ]; then echo 8589934592; exit 0; fi\n'
        'echo "fake docker: $*" >&2; exit 1\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    ("target", "hang"),
    [("langfuse-up", "info"), ("langfuse-up", "stats"), ("langfuse-down", "info")],
)
def test_a_hung_docker_daemon_fails_the_target_clearly(
    tmp_path: Path, target: str, hang: str
) -> None:
    env = {
        **os.environ,
        "DOCKER": str(fake_docker(tmp_path, hang)),
        "ERRAND_DOCKER_TIMEOUT_S": str(TIMEOUT_S),
    }
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=GIVE_UP_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"make {target} hung on `docker {hang}` for {GIVE_UP_S}s")
    took = time.monotonic() - started
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not answering" in result.stderr
    assert f"docker {hang}" in result.stderr
    assert "Traceback" not in result.stderr
    assert took < TIMEOUT_S + 15
