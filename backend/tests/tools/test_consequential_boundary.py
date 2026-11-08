"""book, cancel and reschedule sit behind one module that only the executor may import.

Three layers, each tested here:
- the source tree: `errand.execution.executor` is the only module that imports
  `errand.tools.consequential` (an AST scan, so a new module cannot slip past);
- import-linter: the contract in pyproject.toml is green on the real tree and turns red
  when the consequential module is imported from `agent/nodes/respond.py` or `api/`
  (run on a copy of the source with the mutation applied);
- the ToolGateway: its ordinary path refuses the three side effects for every caller,
  the executor included, and the consequential path refuses every caller but the
  executor, before anything reaches the world.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from errand.clock import SimClock
from errand.domain.counters import RunCounters
from errand.tools.errors import PlannerToolRejected
from errand.tools.gateway import ALLOWED, SIDE_EFFECT_TOOLS, Caller, ToolGateway
from errand.trace.sinks.null import NullSink
from errand.trace.tracer import Tracer

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src"
CONSEQUENTIAL = "errand.tools.consequential"
LINT_IMPORTS = str(Path(sys.executable).parent / "lint-imports")


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def test_only_the_executor_imports_the_consequential_module() -> None:
    importers = sorted(
        module_name(path)
        for path in SRC.rglob("*.py")
        if any(m == CONSEQUENTIAL or m.startswith(CONSEQUENTIAL + ".") for m in imports_of(path))
    )
    assert importers == ["errand.execution.executor"]


def lint_imports(src: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(src)}
    return subprocess.run(
        [LINT_IMPORTS, "--config", str(BACKEND / "pyproject.toml"), "--no-cache"],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND,
        timeout=120,
    )


@pytest.mark.parametrize(
    "mutated",
    ["errand/agent/nodes/respond.py", "errand/api/app.py"],
)
def test_lint_imports_turns_red_when_anything_else_imports_it(tmp_path: Path, mutated: str) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    target = copy / mutated
    target.write_text(
        target.read_text(encoding="utf-8") + f"\nimport {CONSEQUENTIAL}  # mutation\n",
        encoding="utf-8",
    )
    result = lint_imports(copy)
    assert result.returncode != 0, result.stdout
    assert "Only the executor reaches book, cancel and reschedule" in result.stdout
    assert "BROKEN" in result.stdout


def gateway() -> ToolGateway:
    from datetime import datetime

    from errand.clock import IST

    clock = SimClock(datetime(2026, 11, 9, 8, 0, tzinfo=IST))
    tracer = Tracer(run_id="r_t", thread_id="c_t", sinks=[NullSink()], now=clock.now)

    class NoWorld:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the world was reached: {name}")

    return ToolGateway(NoWorld(), tracer=tracer, clock=clock, counters=RunCounters())  # type: ignore[arg-type]


BOOK_ARGS = {"hold_id": "H-1", "idempotency_key": "k"}


@pytest.mark.parametrize("caller", list(Caller))
@pytest.mark.parametrize("tool", sorted(SIDE_EFFECT_TOOLS))
async def test_the_ordinary_path_refuses_side_effects_for_every_caller(
    caller: Caller, tool: str
) -> None:
    assert tool not in ALLOWED[caller]
    with pytest.raises(PlannerToolRejected):
        await gateway().call(caller, tool, BOOK_ARGS)  # type: ignore[arg-type]


@pytest.mark.parametrize("caller", [c for c in Caller if c is not Caller.EXECUTOR])
async def test_the_consequential_path_refuses_every_caller_but_the_executor(
    caller: Caller,
) -> None:
    with pytest.raises(PlannerToolRejected):
        await gateway()._consequential(caller, "book", BOOK_ARGS)


async def test_the_consequential_path_refuses_read_tools_even_for_the_executor() -> None:
    with pytest.raises(PlannerToolRejected):
        await gateway()._consequential(Caller.EXECUTOR, "search_providers", {})
