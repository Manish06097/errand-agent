"""Scan files for credentials before they reach git.

Usage:
    secret_scan.py --tracked     every file in `git ls-files` (make check)
    secret_scan.py FILE...       the given files (pre-commit passes staged files)

What it rejects:
  - Anthropic API keys and Langfuse public or secret keys, by their exact shape;
  - a few other unambiguous credential shapes (AWS access key ids, GitHub tokens,
    PEM private keys);
  - any .env file (.env, .env.local, ...) other than an .env.example template;
  - the literal value of ANTHROPIC_API_KEY (and any other *_KEY, *_SECRET, *_TOKEN or
    *_PASSWORD) from the local, gitignored .env, wherever it appears, binaries included.

Patterns are deliberately specific rather than entropy-based, so the upstream
Langfuse compose file with its CHANGEME defaults needs no suppression. Findings print
the file, line and rule only; a matched value is never printed. The key prefixes are
assembled at runtime so this file and its tests never contain a key-shaped string.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hygiene_common import is_binary, read_regular_file, report, resolve_targets

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

RULES: dict[str, re.Pattern[str]] = {
    "anthropic-api-key": re.compile("sk-" + "ant-" + r"[A-Za-z0-9_\-]{20,}"),
    "langfuse-secret-key": re.compile(r"\bsk-" + "lf-" + _UUID),
    "langfuse-public-key": re.compile(r"\bpk-" + "lf-" + _UUID),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "private-key": re.compile("-----BEGIN " + r"(?:[A-Z]+ )?PRIVATE KEY-----"),
}

LOCAL_ENV = Path(".env")
_SECRET_NAME = re.compile(r"(^ANTHROPIC_API_KEY$|_KEY$|_SECRET$|_TOKEN$|_PASSWORD$)")
# Shorter values are too likely to occur by chance to be worth matching.
_MIN_SECRET_LEN = 16


def is_env_file(path: Path) -> bool:
    name = path.name
    if name.endswith(".example"):
        return False
    return name == ".env" or name.startswith(".env.")


def local_env_secrets(env_path: Path = LOCAL_ENV) -> dict[str, bytes]:
    """Secret-looking values from the local .env, keyed by variable name.

    A minimal KEY=VALUE parser (comments, `export`, single or double quotes) so the
    scanner needs no third-party package.
    """
    data = read_regular_file(env_path)
    if data is None:
        return {}
    secrets: dict[str, bytes] = {}
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if _SECRET_NAME.search(name) and len(value) >= _MIN_SECRET_LEN:
            secrets[name] = value.encode("utf-8")
    return secrets


def scan(paths: list[Path], env_secrets: dict[str, bytes]) -> list[str]:
    problems: list[str] = []
    for path in paths:
        if is_env_file(path):
            problems.append(f"{path}: .env files must never be committed")
            continue
        data = read_regular_file(path)
        if data is None:
            continue
        for name, value in env_secrets.items():
            if value in data:
                problems.append(f"{path}: contains the value of {name} from the local .env")
        if is_binary(data):
            continue
        text = data.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: {rule}")
    return problems


def main(argv: list[str]) -> int:
    paths = resolve_targets(argv)
    problems = scan(paths, local_env_secrets())
    if not problems and argv == ["--tracked"]:
        print(f"secret scan: {len(paths)} tracked file(s) clean")
    return report(
        problems,
        what="secret scan",
        hint="Remove the credential and keep real values only in the gitignored .env.",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
