"""Helpers shared by the repo hygiene guards.

Standard library only: a grader needs nothing but uv to run every guard. Paths are
resolved against the current working directory, which is the repo root both under
`make check` and under pre-commit.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

# Same heuristic git uses: a NUL byte in the first 8000 bytes means binary.
_BINARY_SNIFF_BYTES = 8000


def tracked_files() -> list[Path]:
    """Every path in the index, as git ls-files reports it."""
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout.decode(
        "utf-8", errors="surrogateescape"
    )
    return [Path(p) for p in out.split("\0") if p]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_SNIFF_BYTES]


def read_regular_file(path: Path) -> bytes | None:
    """File contents, or None for anything that is not a readable regular file.

    Symlinks are skipped on purpose: their target is checked on its own if it is
    tracked, and following one could read outside the repo.
    """
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def iter_text_files(paths: Iterable[Path]) -> Iterator[tuple[Path, str]]:
    """(path, decoded text) for every non-binary regular file in `paths`."""
    for path in paths:
        data = read_regular_file(path)
        if data is None or is_binary(data):
            continue
        yield path, data.decode("utf-8", errors="replace")


def resolve_targets(argv: list[str]) -> list[Path]:
    """`--tracked` means every tracked file; otherwise the paths given (pre-commit)."""
    if argv == ["--tracked"]:
        return tracked_files()
    if "--tracked" in argv:
        sys.exit("--tracked takes no other arguments")
    return [Path(a) for a in argv]


def report(problems: list[str], *, what: str, hint: str) -> int:
    if not problems:
        return 0
    print(f"{what}: {len(problems)} problem(s)", file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    print(hint, file=sys.stderr)
    return 1
