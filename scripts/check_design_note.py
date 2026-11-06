"""Enforce the word budget of docs/DESIGN_NOTE.md.

Usage:
    check_design_note.py [PATH]      default: docs/DESIGN_NOTE.md

The note must contain these sections as `## ` headings, in this order:

    State model, Approval, Idempotency and recovery, Memory, Observability,
    Trade-offs, Trace excerpt

Each heading is followed by a budget comment, `<!-- budget: N words -->`. The check
fails if a section is missing, duplicated, out of order or has no budget; if a
section's prose is over its budget; if the declared budgets sum to more than
TOTAL_BUDGET; if the prose of the whole note (preamble included) is over TOTAL_BUDGET;
or if the trace excerpt has more than one fenced code block or one longer than
EXCERPT_MAX_LINES lines. Only the Trace excerpt section may contain a code block; it
has none until the real trace file exists (M17).

How words are counted (deterministic, no locale or library dependence):
  1. HTML comments are removed, including the budget comments.
  2. Fenced code blocks (``` or ~~~) are removed from the prose and measured
     separately, in lines, excluding the fence lines.
  3. The `#` and `##` heading lines themselves are not counted.
  4. A Markdown link or image `[text](target)` counts as its text only.
  5. The remaining text is split on Unicode whitespace. A word is a resulting token
     that contains at least one letter or digit, so list markers, table pipes and
     stray punctuation are not words, while `inline_code()`, "INV-03" and "12k" each
     count as one.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("docs/DESIGN_NOTE.md")
SECTIONS = (
    "State model",
    "Approval",
    "Idempotency and recovery",
    "Memory",
    "Observability",
    "Trade-offs",
    "Trace excerpt",
)
EXCERPT_SECTION = "Trace excerpt"
TOTAL_BUDGET = 550
EXCERPT_MAX_LINES = 12

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BUDGET = re.compile(r"<!--\s*budget:\s*(\d+)\s+words\s*-->")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HAS_ALNUM = re.compile(r"[^\W_]", re.UNICODE)


def count_words(text: str) -> int:
    text = _LINK.sub(r"\1", text)
    return sum(1 for token in text.split() if _HAS_ALNUM.search(token))


@dataclass
class Section:
    title: str
    budget: int | None = None
    prose: list[str] = field(default_factory=list)
    code_blocks: list[int] = field(default_factory=list)  # line count per block

    @property
    def words(self) -> int:
        return count_words("\n".join(self.prose))


def parse(text: str) -> tuple[Section, list[Section], list[str]]:
    """Split the note into a preamble and `## ` sections; also return format errors."""
    errors: list[str] = []
    preamble = Section("(preamble)")
    sections: list[Section] = []
    current = preamble
    in_fence = False
    fence_lines = 0
    # Budget comments are read before comments are stripped from the prose.
    for line in text.splitlines():
        if in_fence:
            if _FENCE.match(line):
                in_fence = False
                current.code_blocks.append(fence_lines)
            else:
                fence_lines += 1
            continue
        if _FENCE.match(line):
            in_fence, fence_lines = True, 0
            continue
        if line.startswith("## "):
            current = Section(line[3:].strip())
            sections.append(current)
            continue
        if line.startswith("# "):
            continue
        budget = _BUDGET.search(line)
        if budget:
            if current is preamble:
                errors.append("budget comment outside any section")
            elif current.budget is not None:
                errors.append(f"{current.title}: more than one budget comment")
            else:
                current.budget = int(budget.group(1))
        current.prose.append(line)
    if in_fence:
        errors.append(f"{current.title}: unclosed code block")
    for section in [preamble, *sections]:
        section.prose = _COMMENT.sub(" ", "\n".join(section.prose)).splitlines()
    return preamble, sections, errors


def check(text: str) -> list[str]:
    preamble, sections, errors = parse(text)
    titles = [s.title for s in sections]
    for title in SECTIONS:
        if titles.count(title) == 0:
            errors.append(f"missing section: ## {title}")
        elif titles.count(title) > 1:
            errors.append(f"duplicated section: ## {title}")
    unknown = [t for t in titles if t not in SECTIONS]
    if unknown:
        errors.append(f"unexpected section(s): {', '.join(unknown)}")
    known = [t for t in titles if t in SECTIONS]
    if not errors and known != list(SECTIONS):
        errors.append("sections out of order; expected: " + ", ".join(SECTIONS))

    declared = 0
    for section in sections:
        if section.budget is None:
            errors.append(f"{section.title}: no <!-- budget: N words --> comment")
            continue
        declared += section.budget
        if section.words > section.budget:
            errors.append(
                f"{section.title}: {section.words} words, over its budget of {section.budget}"
            )
        if section.code_blocks and section.title != EXCERPT_SECTION:
            errors.append(f"{section.title}: code blocks belong only in {EXCERPT_SECTION}")
        if section.title == EXCERPT_SECTION:
            if len(section.code_blocks) > 1:
                errors.append(
                    f"{EXCERPT_SECTION}: expected at most one code block, "
                    f"found {len(section.code_blocks)}"
                )
            elif section.code_blocks and section.code_blocks[0] > EXCERPT_MAX_LINES:
                errors.append(
                    f"{EXCERPT_SECTION}: code block is {section.code_blocks[0]} lines, "
                    f"over the cap of {EXCERPT_MAX_LINES}"
                )
    if preamble.code_blocks:
        errors.append(f"(preamble): code blocks belong only in {EXCERPT_SECTION}")
    if declared > TOTAL_BUDGET:
        errors.append(f"declared budgets sum to {declared}, over the total of {TOTAL_BUDGET}")
    total = preamble.words + sum(s.words for s in sections)
    if total > TOTAL_BUDGET:
        errors.append(f"note is {total} words of prose, over the total of {TOTAL_BUDGET}")
    return errors


def summary(text: str) -> str:
    preamble, sections, _ = parse(text)
    parts = [f"{s.title} {s.words}/{s.budget}" for s in sections]
    total = preamble.words + sum(s.words for s in sections)
    return f"design note: {total}/{TOTAL_BUDGET} words ({'; '.join(parts)})"


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        sys.exit("usage: check_design_note.py [PATH]")
    path = Path(argv[0]) if argv else DEFAULT_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"design note check: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    errors = check(text)
    if errors:
        print(f"design note check failed for {path}:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(summary(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
