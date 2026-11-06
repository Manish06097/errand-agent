"""Run errand-world as a real subprocess on port 0 and learn the port it bound."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

READY_TIMEOUT_S = 20.0
LISTENING = "errand-world listening on "


@dataclass
class WorldProcess:
    proc: subprocess.Popen[str]
    base_url: str
    output: list[str] = field(default_factory=list)

    def stop(self, kill: bool = False) -> None:
        if self.proc.poll() is None:
            if kill:
                self.proc.kill()
            else:
                self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def world_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env["ERRAND_DEV"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start_world(db: Path, *args: str) -> WorldProcess:
    """Start a world on `db`; raise with its output if it does not come up."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "errand.world.server", "--port", "0", "--db", str(db), *args],
        env=world_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    output: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=READY_TIMEOUT_S)
        except queue.Empty:
            proc.kill()
            raise RuntimeError(
                "errand-world did not report its port:\n" + "".join(output)
            ) from None
        if line is None:
            proc.wait()
            raise RuntimeError(f"errand-world exited with {proc.returncode}:\n" + "".join(output))
        if line.startswith(LISTENING):
            return WorldProcess(proc, line[len(LISTENING) :].strip(), output)
