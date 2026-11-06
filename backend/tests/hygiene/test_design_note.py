"""check_design_note.py, run as `make check` runs it, against the real note and broken copies."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.hygiene.repo import CLEAN_DESIGN_NOTE, REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check_design_note.py"
NOTE = CLEAN_DESIGN_NOTE.read_text(encoding="utf-8")


def run_check(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    note = tmp_path / "DESIGN_NOTE.md"
    note.write_text(text, encoding="utf-8")
    return subprocess.run(
        ["python", str(SCRIPT), str(note)], capture_output=True, text=True, cwd=tmp_path
    )


def add_to(section: str, text: str, note: str = NOTE) -> str:
    marker = f"## {section}\n"
    budget_end = note.index("-->", note.index(marker)) + len("-->\n")
    return note[:budget_end] + text + "\n" + note[budget_end:]


def words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_real_note_passes() -> None:
    result = subprocess.run(["python", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("design note: ")


def test_section_exactly_at_budget_passes(tmp_path: Path) -> None:
    assert run_check(tmp_path, add_to("Memory", words(80))).returncode == 0


def test_section_over_budget_fails(tmp_path: Path) -> None:
    result = run_check(tmp_path, add_to("Memory", words(81)))
    assert result.returncode == 1
    assert "Memory: 81 words, over its budget of 80" in result.stderr


def test_total_over_550_fails_even_if_sections_fit(tmp_path: Path) -> None:
    # Preamble words count toward the total; the sections are untouched.
    text = NOTE.replace("\n## State model", words(1) + "\n\n## State model", 1)
    for section, budget in [
        ("State model", 90),
        ("Approval", 90),
        ("Idempotency and recovery", 100),
        ("Memory", 80),
        ("Observability", 70),
        ("Trade-offs", 80),
        ("Trace excerpt", 40),
    ]:
        text = add_to(section, words(budget), text)
    result = run_check(tmp_path, text)
    assert result.returncode == 1
    assert "note is 551 words of prose, over the total of 550" in result.stderr


def test_declared_budgets_over_550_fail(tmp_path: Path) -> None:
    text = NOTE.replace("<!-- budget: 90 words -->", "<!-- budget: 91 words -->", 1)
    result = run_check(tmp_path, text)
    assert result.returncode == 1
    assert "declared budgets sum to 551" in result.stderr


def test_missing_section_fails(tmp_path: Path) -> None:
    text = NOTE.replace("## Observability\n<!-- budget: 70 words -->\n", "")
    result = run_check(tmp_path, text)
    assert result.returncode == 1
    assert "missing section: ## Observability" in result.stderr


def test_section_without_budget_fails(tmp_path: Path) -> None:
    text = NOTE.replace("## Memory\n<!-- budget: 80 words -->", "## Memory")
    result = run_check(tmp_path, text)
    assert result.returncode == 1
    assert "Memory: no <!-- budget: N words --> comment" in result.stderr


def test_sections_out_of_order_fail(tmp_path: Path) -> None:
    a = "## Memory\n<!-- budget: 80 words -->\n"
    b = "## Observability\n<!-- budget: 70 words -->\n"
    text = NOTE.replace(a, "@@A@@").replace(b, a).replace("@@A@@", b)
    result = run_check(tmp_path, text)
    assert result.returncode == 1
    assert "sections out of order" in result.stderr


def fence(lines: int) -> str:
    body = "\n".join(f'{{"seq": {i}}}' for i in range(lines))
    return "```json\n" + body + "\n```"


def test_excerpt_of_12_lines_passes(tmp_path: Path) -> None:
    assert run_check(tmp_path, add_to("Trace excerpt", fence(12))).returncode == 0


def test_excerpt_of_13_lines_fails(tmp_path: Path) -> None:
    result = run_check(tmp_path, add_to("Trace excerpt", fence(13)))
    assert result.returncode == 1
    assert "code block is 13 lines, over the cap of 12" in result.stderr


def test_code_outside_the_excerpt_fails(tmp_path: Path) -> None:
    result = run_check(tmp_path, add_to("Approval", fence(2)))
    assert result.returncode == 1
    assert "Approval: code blocks belong only in Trace excerpt" in result.stderr


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain words only", 3),
        ("- a list item | with a pipe | and `inline_code()`", 8),
        ("see [the trace](docs/traces/x.jsonl) now", 4),
        ("<!-- a comment is not prose --> two words", 2),
        ("INV-03 holds at 12k tokens; costs $0.0002 - fine.", 8),
    ],
)
def test_word_counting_rules(tmp_path: Path, text: str, expected: int) -> None:
    result = run_check(tmp_path, add_to("Memory", text))
    assert result.returncode == 0, result.stderr
    assert f"Memory {expected}/80" in result.stdout
