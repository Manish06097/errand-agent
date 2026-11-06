"""BUG-M2-1: filling a design-note section, as every milestone must, turns `make check` red.

tests/hygiene/test_design_note.py built its broken notes by adding words to the real
docs/DESIGN_NOTE.md, so it silently assumed every section was still empty. M2 filled
the Observability section (65 of 70 words) and the "total over 550" and other tests
started failing, although the note itself passes check_design_note.py.

The repro copies the hygiene tests and the checker into a temporary tree laid out like
the repo, fills every section of the note the way the milestones will, and runs those
tests with pytest in a subprocess, exactly as `make check` would run them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from tests.hygiene.repo import REPO_ROOT

SECTIONS = (
    "State model",
    "Approval",
    "Idempotency and recovery",
    "Memory",
    "Observability",
    "Trade-offs",
    "Trace excerpt",
)
COPIED = (
    "backend/tests/__init__.py",
    "backend/tests/hygiene/__init__.py",
    "backend/tests/hygiene/conftest.py",
    "backend/tests/hygiene/repo.py",
    "backend/tests/hygiene/test_design_note.py",
    "scripts/check_design_note.py",
    "docs/DESIGN_NOTE.md",
)


def _filled(note: str) -> str:
    """The note with each section's prose replaced by one sentence, whatever it held."""
    out: list[str] = []
    in_sections = False
    for line in note.splitlines():
        if line.startswith("## "):
            in_sections = True
        elif in_sections and line.strip() and not line.lstrip().startswith("<!--"):
            continue  # existing prose; the repro must not depend on how full the note is
        out.append(line)
        if in_sections and line.startswith("<!-- budget:"):
            section = out[-2][3:]
            out.append(f"Filled in by the milestone that builds {section}.")
    return "\n".join(out) + "\n"


def test_design_note_hygiene_tests_pass_once_sections_are_filled(tmp_path: Path) -> None:
    for rel in COPIED:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    (tmp_path / "backend" / "pytest.ini").write_text("[pytest]\n")
    note = tmp_path / "docs" / "DESIGN_NOTE.md"
    note.write_text(_filled(note.read_text(encoding="utf-8")), encoding="utf-8")

    checker = subprocess.run(
        [sys.executable, "scripts/check_design_note.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert checker.returncode == 0, checker.stderr  # the filled note itself is valid

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "backend" / "pytest.ini"),
            "--rootdir",
            str(tmp_path / "backend"),
            str(tmp_path / "backend" / "tests" / "hygiene" / "test_design_note.py"),
        ],
        cwd=tmp_path / "backend",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
