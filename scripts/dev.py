"""`make dev`: run the mock world, the API and the web app together.

- Ports come from ERRAND_WORLD_PORT, ERRAND_API_PORT and ERRAND_WEB_PORT (defaults
  8765, 8000, 3200). Every port is checked before anything starts, and a taken port
  fails with the variable to set, rather than a stack trace from a half-started stack.
- The web app's /api rewrite follows the API port: ERRAND_API_URL defaults to
  http://127.0.0.1:$ERRAND_API_PORT unless it is set explicitly.
- The world serves its /dev controls (ERRAND_DEV=1 unless set) and keeps its store in
  data/world.sqlite (ERRAND_WORLD_DB), loading bangalore_v1 the first time.
- The API reaches that world over HTTP (ERRAND_WORLD_URL, derived from the world port
  unless set) and, in scripted mode, plans with the eval suite's reference policy, so
  backend/ is put on its PYTHONPATH for `evals.policies.reference`.
- Each process's output is prefixed with its name.
- If any process exits, or on Ctrl-C, all of them are stopped. Each child runs in its
  own process group so pnpm's node and Next's workers go down with it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

STOP_GRACE_S = 5.0
READY_TIMEOUT_S = 90.0
COLORS = {"world": "\033[35m", "api": "\033[36m", "web": "\033[32m", "dev": "\033[33m"}
RESET = "\033[0m"

_print_lock = threading.Lock()
_use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def log(name: str, line: str) -> None:
    tag = f"[{name}]".ljust(8)
    if _use_color:
        tag = f"{COLORS.get(name, '')}{tag}{RESET}"
    with _print_lock:
        sys.stdout.write(f"{tag}{line}\n")
        sys.stdout.flush()


@dataclass
class Service:
    name: str
    env_var: str
    default_port: int
    health_path: str
    cmd: list[str] = field(default_factory=list)
    proc: subprocess.Popen[bytes] | None = None

    @property
    def port(self) -> int:
        raw = os.environ.get(self.env_var, str(self.default_port))
        try:
            port = int(raw)
        except ValueError:
            sys.exit(f"[dev] {self.env_var}={raw!r} is not a port number")
        if not 0 < port < 65536:
            sys.exit(f"[dev] {self.env_var}={port} is out of range")
        return port


def port_holder(port: int) -> str:
    """Best-effort name of the process listening on `port`, for the error message."""
    if not shutil.which("lsof"):
        return ""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    names = sorted({line[1:] for line in out.splitlines() if line.startswith("c")})
    return ", ".join(names)


def port_in_use(port: int) -> bool:
    # Something accepting on loopback is the common case (Docker publishes on *:port).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    # Also catch a listener bound but not accepting, or bound to another interface.
    for family, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                # Same option uvicorn and Next set, so TIME_WAIT leftovers from the
                # last run do not read as a taken port.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
    return False


def preflight(services: list[Service]) -> None:
    ports = [s.port for s in services]
    if len(set(ports)) != len(ports):
        sys.exit("[dev] two services are configured on the same port: " + str(ports))
    failed = False
    for svc in services:
        if port_in_use(svc.port):
            holder = port_holder(svc.port)
            held_by = f" (held by {holder})" if holder else ""
            print(
                f"[dev] port {svc.port} for {svc.name} is already in use{held_by}.\n"
                f"[dev]   Pick a free port with {svc.env_var}, for example: "
                f"{svc.env_var}={svc.port + 100} make dev",
                file=sys.stderr,
            )
            failed = True
    if failed:
        sys.exit(1)


def pump(name: str, stream: IO[bytes]) -> None:
    for raw in iter(stream.readline, b""):
        log(name, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    stream.close()


def start(svc: Service, env: dict[str, str]) -> None:
    log("dev", f"starting {svc.name} on port {svc.port}: {' '.join(svc.cmd)}")
    svc.proc = subprocess.Popen(
        svc.cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert svc.proc.stdout is not None
    threading.Thread(target=pump, args=(svc.name, svc.proc.stdout), daemon=True).start()


def stop_all(services: list[Service]) -> None:
    running = [s for s in services if s.proc and s.proc.poll() is None]
    for svc in running:
        assert svc.proc is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(svc.proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_S
    for svc in running:
        assert svc.proc is not None
        try:
            svc.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log("dev", f"{svc.name} did not stop in {STOP_GRACE_S:.0f}s, killing it")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(svc.proc.pid, signal.SIGKILL)
            svc.proc.wait()
    # A process group can outlive its leader (Next spawns workers); clean those up too.
    for svc in services:
        if svc.proc:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(svc.proc.pid, signal.SIGKILL)


def wait_ready(services: list[Service], stop: threading.Event) -> None:
    pending = {s.name: f"http://127.0.0.1:{s.port}{s.health_path}" for s in services}
    deadline = time.monotonic() + READY_TIMEOUT_S
    while pending and not stop.is_set() and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        del pending[name]
            except OSError:
                pass
        stop.wait(0.5)
    if stop.is_set():
        return
    if pending:
        log("dev", f"not healthy after {READY_TIMEOUT_S:.0f}s: {', '.join(sorted(pending))}")
        return
    web = next(s for s in services if s.name == "web")
    log("dev", f"ready: open http://localhost:{web.port} (Ctrl-C stops everything)")


def main() -> int:
    world = Service("world", "ERRAND_WORLD_PORT", 8765, "/health")
    api = Service("api", "ERRAND_API_PORT", 8000, "/api/health")
    web = Service("web", "ERRAND_WEB_PORT", 3200, "/api/health")
    services = [world, api, web]
    preflight(services)

    uv = ["uv", "run", "--frozen", "--project", "backend"]
    world.cmd = [*uv, "errand-world"]
    api.cmd = [*uv, "errand-api"]
    web.cmd = ["pnpm", "-C", "web", "exec", "next", "dev", "-p", str(web.port)]

    env = dict(os.environ)
    env[world.env_var] = str(world.port)
    env[api.env_var] = str(api.port)
    env[web.env_var] = str(web.port)
    env.setdefault("ERRAND_API_URL", f"http://127.0.0.1:{api.port}")
    env.setdefault("ERRAND_WORLD_URL", f"http://127.0.0.1:{world.port}")
    backend = str(Path(__file__).resolve().parent.parent / "backend")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (backend, env.get("PYTHONPATH")) if p)
    env.setdefault("ERRAND_DEV", "1")
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    log("dev", f"web rewrites /api/* to {env['ERRAND_API_URL']}")

    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    exit_code = 0
    try:
        for svc in services:
            start(svc, env)
        threading.Thread(target=wait_ready, args=(services, stop), daemon=True).start()
        while not stop.is_set():
            for svc in services:
                assert svc.proc is not None
                code = svc.proc.poll()
                if code is not None:
                    log("dev", f"{svc.name} exited with code {code}; stopping the others")
                    exit_code = code or 1
                    stop.set()
                    break
            stop.wait(0.2)
        if exit_code == 0:
            log("dev", "stopping")
            exit_code = 130
    finally:
        stop.set()
        stop_all(services)
    log("dev", "all stopped")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
