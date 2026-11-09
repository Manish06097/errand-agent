"""The transcript export redacts every key, and the em-dash exemption is that one file.

Key-shaped strings are built by concatenation so this file never trips the secret scan.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.hygiene.repo import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
EM_DASH = chr(0x2014)
LITERAL_KEY = "sk-" + "ant-" + "api03-" + "L" * 40
SHAPED_KEY = "sk-" + "ant-" + "admin01-" + "S" * 30
LANGFUSE_KEY = "pk-" + "lf-" + "12345678-1234-1234-1234-123456789abc"


def run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def write_session(root: Path) -> None:
    records = [
        {
            "type": "user",
            "timestamp": "2026-11-06T11:10:02.000Z",
            "message": {"role": "user", "content": f"here is my key {LITERAL_KEY} go"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-06T11:11:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Noted {EM_DASH} starting."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": f"echo {SHAPED_KEY}"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-06T11:12:00.000Z",
            "message": {"role": "user", "content": f"trace dump public_key {LANGFUSE_KEY}"},
        },
    ]
    (root / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_the_export_is_one_file_with_every_key_redacted(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_session(src)
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY={LITERAL_KEY}\n", encoding="utf-8")
    out = tmp_path / "docs" / "AI_TRANSCRIPT.md"

    result = run(
        "export_transcripts.py",
        str(src),
        "sess-1",
        "--out",
        str(out),
        "--env",
        str(env),
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert [p.name for p in (tmp_path / "docs").iterdir()] == ["AI_TRANSCRIPT.md"]
    body = out.read_text(encoding="utf-8")
    for secret in (LITERAL_KEY, SHAPED_KEY, LANGFUSE_KEY):
        assert secret not in body
    assert "here is my key [REDACTED-KEY] go" in body
    assert "### User (2026-11-06 11:10:02 UTC)" in body
    assert "**Tool call: Bash**" in body
    scan = run("secret_scan.py", "docs/AI_TRANSCRIPT.md", cwd=tmp_path)
    assert scan.returncode == 0, scan.stderr


def test_the_em_dash_exemption_covers_only_the_transcript(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI_TRANSCRIPT.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.md").write_text(f"a{EM_DASH}b\n", encoding="utf-8")

    exempt = run("check_em_dash.py", "docs/AI_TRANSCRIPT.md", cwd=tmp_path)
    rejected = run("check_em_dash.py", "docs/notes.md", cwd=tmp_path)

    assert exempt.returncode == 0, exempt.stderr
    assert rejected.returncode == 1
    assert "docs/notes.md:1:2" in rejected.stderr
