"""Run the offline eval suite (pytest over backend/evals).

pytest exits 5 when it collects no tests. Until the first scenario lands (M3) that is
the expected state, so exit 5 is reported as "0 scenarios" and exits 0, but only when
backend/evals really holds nothing besides placeholders: `.gitkeep`, `__init__.py`
and `__pycache__/` directories. If anything else is there (a misnamed scenario file,
a conftest whose collection hook broke), collecting nothing is a mistake, so the
wrapper says what it found and exits 1. With extra pytest arguments (a -k filter,
say) an empty selection is also a mistake, and exit 5 passes through. Every other
exit code passes through unchanged.

ERRAND_EVALS_DIR points the wrapper at another evals directory; the tests use it to
run against a temp copy instead of the real tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
NO_TESTS_COLLECTED = 5
PLACEHOLDER_FILES = frozenset({".gitkeep", "__init__.py"})
PLACEHOLDER_DIRS = frozenset({"__pycache__"})


def unexpected_entries(evals_dir: Path) -> list[str]:
    """Paths under evals_dir that are not placeholders, relative and sorted."""
    found: list[str] = []
    for root, dirs, files in os.walk(evals_dir):
        dirs[:] = [d for d in dirs if d not in PLACEHOLDER_DIRS]
        base = Path(root)
        found += [str((base / d).relative_to(evals_dir)) + "/" for d in dirs]
        found += [
            str((base / f).relative_to(evals_dir)) for f in files if f not in PLACEHOLDER_FILES
        ]
    return sorted(found)


def main(argv: list[str]) -> int:
    evals_dir = Path(os.environ.get("ERRAND_EVALS_DIR") or BACKEND / "evals").resolve()
    # Pin the config and rootdir so a temp evals dir runs under the same pytest
    # settings (markers, asyncio mode) as the real one.
    cmd = [
        *("uv", "run", "--frozen", "--project", str(BACKEND), "pytest"),
        *("-c", str(BACKEND / "pyproject.toml"), "--rootdir", str(BACKEND)),
        str(evals_dir),
        *argv,
    ]
    code = subprocess.run(cmd, cwd=BACKEND).returncode
    if code != NO_TESTS_COLLECTED or argv:
        return code
    leftovers = unexpected_entries(evals_dir)
    if leftovers:
        print(
            f"pytest collected no scenarios, but {evals_dir} is not empty: " + ", ".join(leftovers),
            file=sys.stderr,
        )
        return 1
    print("0 scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
