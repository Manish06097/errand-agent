"""Run the offline eval suite (pytest over backend/evals).

Strict since the first scenarios landed (M3): pytest's exit code passes through
unchanged, so an empty collection (exit 5) fails `make evals` like any other failure.
A misnamed scenario file or a conftest whose collection broke can no longer pass as
"nothing to evaluate".

`--write-report` (what `make evals` passes) regenerates docs/EVAL_TABLE.md and
docs/eval_results.json through the report plugin; without it nothing under docs/ is
touched, so a filtered or ad hoc run cannot overwrite the committed table.

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
WRITE_REPORT = "--write-report"
REPORT_DIR_ENV = "ERRAND_EVAL_REPORT_DIR"


def main(argv: list[str]) -> int:
    evals_dir = Path(os.environ.get("ERRAND_EVALS_DIR") or BACKEND / "evals").resolve()
    write_report = WRITE_REPORT in argv
    args = [a for a in argv if a != WRITE_REPORT]
    env = dict(os.environ)
    env.pop(REPORT_DIR_ENV, None)
    if write_report:
        env[REPORT_DIR_ENV] = str(REPO / "docs")
    # Pin the config and rootdir so a temp evals dir runs under the same pytest
    # settings (markers, asyncio mode) as the real one.
    cmd = [
        *("uv", "run", "--frozen", "--project", str(BACKEND), "pytest"),
        *("-c", str(BACKEND / "pyproject.toml"), "--rootdir", str(BACKEND)),
        str(evals_dir),
        *args,
    ]
    code = subprocess.run(cmd, cwd=BACKEND, env=env).returncode
    if code == NO_TESTS_COLLECTED:
        print(
            f"no scenarios collected under {evals_dir}; make evals fails on that", file=sys.stderr
        )
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
