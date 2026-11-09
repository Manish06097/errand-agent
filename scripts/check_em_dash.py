"""Reject em dashes in text files.

Usage:
    check_em_dash.py --tracked        every file in `git ls-files` (make check)
    check_em_dash.py FILE...          the given files (pre-commit passes staged files)

The coding-assistant transcript, docs/AI_TRANSCRIPT.md, is exempt: it quotes the
session as written. Nothing else is exempt.

Rejects the U+2014 character and the HTML entities that render as one. Binary files
(a NUL byte in the first 8000 bytes) are skipped. The forbidden strings are built
with chr() and concatenation so this file never trips over itself.
"""

from __future__ import annotations

import sys

from hygiene_common import iter_text_files, report, resolve_targets

EM_DASH = chr(0x2014)
FORBIDDEN = {
    EM_DASH: "U+2014 em dash",
    "&" + "mdash;": "&" + "mdash; entity",
    "&#" + "8212;": "&#" + "8212; entity",
    "&#x" + "2014;": "&#x" + "2014; entity",
}
EXEMPT = {"docs/AI_TRANSCRIPT.md"}


def find_problems(argv: list[str]) -> list[str]:
    problems: list[str] = []
    paths = [p for p in resolve_targets(argv) if p.as_posix() not in EXEMPT]
    for path, text in iter_text_files(paths):
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle, label in FORBIDDEN.items():
                col = line.find(needle)
                if col != -1:
                    problems.append(f"{path}:{lineno}:{col + 1}: {label}")
    if not problems and argv == ["--tracked"]:
        print(f"em-dash guard: {len(paths)} tracked file(s) clean")
    return problems


def main(argv: list[str]) -> int:
    return report(
        find_problems(argv),
        what="em-dash guard",
        hint="Use a plain hyphen or rewrite the sentence.",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
