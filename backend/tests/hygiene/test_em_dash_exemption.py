"""The em-dash guard exempts the two coding-assistant transcripts and nothing else."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.hygiene.repo import REPO_ROOT

GUARD = REPO_ROOT / "scripts" / "check_em_dash.py"
EM_DASH = chr(0x2014)


def run_guard(path: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), path], cwd=cwd, capture_output=True, text=True, check=False
    )


def test_the_em_dash_exemption_covers_only_the_transcripts(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI_TRANSCRIPT.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")
    (tmp_path / "AI_TRANSCRIPT.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")
    nested = tmp_path / "docs" / "notes"
    nested.mkdir()
    (nested / "AI_TRANSCRIPT.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")

    for path in ("docs/AI_TRANSCRIPT.md", "AI_TRANSCRIPT.md"):
        exempt = run_guard(path, tmp_path)
        assert exempt.returncode == 0, exempt.stderr
    for path in ("docs/notes.md", "docs/notes/AI_TRANSCRIPT.md"):
        rejected = run_guard(path, tmp_path)
        assert rejected.returncode == 1
        assert f"{path}:1:2" in rejected.stderr
