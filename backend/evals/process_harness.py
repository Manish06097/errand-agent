"""The subprocess harness (architecture 18.1): a real kill, a real restart.

The in-process `Harness` cannot die: `os._exit(1)` would take the test runner with it.
This one starts the mock world as an HTTP process (`errand-world`, a simulated clock, the
scenario loaded through `/dev/scenario`) and the API as its own process on a free port,
both over the scenario's directory, with `ERRAND_FAULT=<point>:exit` set for the first
start only. The scenario talks to the API over real HTTP, the way the web app does.

`restart()` waits for the API to exit with code 1, starts it again on the same data
directory without the fault, and returns once it serves: the lifespan runs the
reconciler to completion before the first request is accepted, so by the time `restart`
returns every recovery run has ended and the conversation shows what the user would see.

It is a `Harness` for everything a scenario step uses (the client, the inspector, the
records), so the runner and the invariants are the ones the other scenarios use.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from errand.clock import TrackingClock
from errand.llm.scripted import Policy
from errand.tools.inspector import HttpInspector
from errand.world.scenario import ScenarioConfig
from evals import invariants
from evals.runner import Harness, RunView

BACKEND = Path(__file__).resolve().parents[1]
READY_TIMEOUT_S = 60.0
EXIT_TIMEOUT_S = 30.0
CRASHED = 599  # what the harness answers when the API died under a request


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _base_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ERRAND_")}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(BACKEND), env.get("PYTHONPATH")) if p)
    return env


class ProcessHarness(Harness):
    """A `Harness` whose world and API are processes this object starts and kills."""

    def __init__(
        self,
        root: Path,
        world: ScenarioConfig,
        policy: Policy,
        *,
        crash_at: str | None,
        reader_name: str = "keyword",
        api_port: int | None = None,
    ) -> None:
        # Not Harness.__init__: there is no in-process world host here.
        self.root = root
        self.world_config = world
        self.policy = policy
        self.reader_name = reader_name
        self.crash_at = crash_at
        self.conversation_id = ""
        self.conversations: dict[str, str] = {}
        self._msg_seq = 0
        self._action_seq = 0
        self.world_proc: subprocess.Popen[bytes] | None = None
        self.api_proc: subprocess.Popen[bytes] | None = None
        self.world_url = ""
        self.api_port = api_port or free_port()
        self.client: httpx.AsyncClient
        self.inspector: HttpInspector  # type: ignore[assignment]
        self.crashed = False
        self.exit_code: int | None = None
        self.starts = 0
        self._logs: list[Any] = []

    # ---------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> ProcessHarness:
        self.root.mkdir(parents=True, exist_ok=True)
        await self._start_world()
        self.inspector = HttpInspector(self.world_url, TrackingClock())
        await self.inspector.load_scenario(self.world_config, "sim")
        await self._start_api(fault=self.crash_at)
        await self.switch("main")
        return self

    async def __aexit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            await self.client.aclose()
        with contextlib.suppress(Exception):
            await self.inspector.__aexit__(None, None, None)
        for proc in (self.api_proc, self.world_proc):
            await asyncio.to_thread(_stop, proc)
        for log in self._logs:
            log.close()

    def _log(self, name: str) -> Any:
        f = (self.root / f"{name}.log").open("ab")
        self._logs.append(f)
        return f

    async def _start_world(self) -> None:
        env = _base_env()
        env["ERRAND_DEV"] = "1"
        self.world_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "errand.world.server",
                "--port",
                "0",
                "--db",
                str(self.root / "world.sqlite"),
                "--clock",
                "sim",
            ],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        self.world_url = await asyncio.to_thread(self._read_world_url)

    def _read_world_url(self) -> str:
        proc = self.world_proc
        assert proc is not None and proc.stdout is not None
        deadline = time.monotonic() + READY_TIMEOUT_S
        out = self._log("world")
        while time.monotonic() < deadline:
            line = proc.stdout.readline().decode(errors="replace")
            if not line:
                raise RuntimeError(f"errand-world exited with {proc.poll()} before listening")
            out.write(line.encode())
            out.flush()
            if line.startswith("errand-world listening on "):
                # Keep draining so the pipe never fills and blocks the world.
                import threading

                def drain() -> None:
                    assert proc.stdout is not None
                    for rest in proc.stdout:
                        out.write(rest)

                threading.Thread(target=drain, daemon=True).start()
                return line.split(" on ", 1)[1].strip()
        raise RuntimeError("errand-world did not report its port")

    async def _start_api(self, *, fault: str | None) -> None:
        env = _base_env()
        env.update(
            {
                "ERRAND_API_PORT": str(self.api_port),
                "ERRAND_DATA_DIR": str(self.root),
                "ERRAND_WORLD_URL": self.world_url,
                "ERRAND_MODEL_MODE": "scripted",
                "ERRAND_SCRIPTED_READER": f"evals.policies.reader:{self.reader_name}",
                "ERRAND_RECONCILER_INTERVAL_S": "0",
                "ERRAND_LANGFUSE": "off",
                "ERRAND_DEV": "1",
            }
        )
        if fault is not None:
            env["ERRAND_FAULT"] = f"{fault}:exit"
        self.exit_code = None
        self.crashed = False
        self.starts += 1
        self.api_proc = subprocess.Popen(
            [sys.executable, "-c", "from errand.api.app import main; main()"],
            cwd=BACKEND,
            env=env,
            stdout=self._log("api"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.api_port}", timeout=httpx.Timeout(120.0)
        )
        await self._wait_ready()

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        proc = self.api_proc
        assert proc is not None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"the API exited with {proc.returncode} while starting; see {self.root}/api.log"
                )
            try:
                response = await self.client.get("/api/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
        raise RuntimeError(f"the API did not serve within {READY_TIMEOUT_S:g}s")

    async def wait_exit(self, timeout: float = EXIT_TIMEOUT_S) -> int | None:
        """The API's exit code once it has exited (None if it is still running)."""
        proc = self.api_proc
        assert proc is not None
        try:
            code = await asyncio.to_thread(proc.wait, timeout)
        except subprocess.TimeoutExpired:
            return None
        self.exit_code = code
        self.crashed = True
        return code

    async def restart(self) -> int | None:
        """Wait for the crash, then start the API again without the fault. Returns the
        exit code the crashed process had (the scenario expects 1)."""
        code = await self.wait_exit()
        with contextlib.suppress(Exception):
            await self.client.aclose()
        if code is None:
            await asyncio.to_thread(_stop, self.api_proc)
        await self._start_api(fault=None)
        return code

    # ------------------------------------------------------------- client side

    @property
    def manager(self) -> Any:  # pragma: no cover - there is no in-process manager
        raise AttributeError("the API is a process; ask it over HTTP")

    async def _guard(self, make: Any) -> httpx.Response:
        try:
            return await make()
        except httpx.TransportError:
            code = await self.wait_exit(10)
            return httpx.Response(CRASHED, json={"error": "agent_crashed", "exit_code": code})

    async def decide(
        self, approval_id: str, decision: str, fingerprint: str, client_action_id: str | None = None
    ) -> httpx.Response:
        return await self._guard(
            lambda: Harness.decide(self, approval_id, decision, fingerprint, client_action_id)
        )

    async def finish(self, run_id: str) -> RunView:
        try:
            return await Harness.finish(self, run_id)
        except httpx.TransportError:
            # The process died under the run: what the user saw is what the transcript holds.
            await self.wait_exit(10)
            return RunView(run_id, None, [], [])

    async def idle(self) -> None:
        """Every conversation has no active run."""
        for cid in list(self.conversations.values()):
            for _ in range(600):
                response = await self.client.get(f"/api/conversations/{cid}")
                if response.json().get("active_run_id") is None:
                    break
                await asyncio.sleep(0.1)

    async def reconcile(self) -> list[str]:
        response = await self.client.post("/api/dev/reconciler/tick", json={})
        response.raise_for_status()
        return [str(r) for r in response.json()["recovery_runs"]]

    async def records(self) -> invariants.Records:
        if self.api_proc is not None and self.api_proc.poll() is None:
            await self.idle()
        return invariants.load_records(self.root, await self.op_log())


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
