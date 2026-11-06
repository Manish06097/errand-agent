"""errand.world may import only errand.clock from the rest of the package.

import-linter forbids the packages that exist today; this test also catches any
package added later (config, store, trace, ...) without anyone remembering to list it.
"""

from __future__ import annotations

import ast
from pathlib import Path

WORLD = Path(__file__).resolve().parents[2] / "src" / "errand" / "world"


def errand_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("errand")}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("errand"):
            found.add(node.module)
    return found


def test_world_imports_only_itself_and_the_clock() -> None:
    files = sorted(WORLD.glob("*.py"))
    assert files
    for path in files:
        for module in errand_imports(path):
            allowed = module == "errand.clock" or module.startswith("errand.world")
            assert allowed, f"{path.name} imports {module}"
