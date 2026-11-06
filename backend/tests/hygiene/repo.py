"""HookedRepo: a throwaway git repo with this repo's real pre-commit hooks installed.

The repo gets a verbatim copy of .pre-commit-config.yaml and symlinks to backend/ and
scripts/, so every hook runs exactly as it does here: the same `uv run --project
backend` entries, the same lockfile, the same guard scripts. Tests then make real
`git commit`s and read git's exit code and output.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLEAN_DESIGN_NOTE = REPO_ROOT / "docs" / "DESIGN_NOTE.md"


def isolated_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PRE_COMMIT"))}
    env.pop("VIRTUAL_ENV", None)
    env.update(
        # Nothing from the developer's git config (hooksPath, signing, templates) leaks in.
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Hygiene Test",
        GIT_AUTHOR_EMAIL="hygiene@example.invalid",
        GIT_COMMITTER_NAME="Hygiene Test",
        GIT_COMMITTER_EMAIL="hygiene@example.invalid",
        PRE_COMMIT_HOME=str(home / "pre-commit-cache"),
        # Hooks must work without the network; this makes uv prove it.
        UV_OFFLINE="1",
        NO_COLOR="1",
    )
    (home / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
    return env


@dataclass
class HookedRepo:
    path: Path
    env: dict[str, str]

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(args), cwd=self.path, env=self.env, capture_output=True, text=True
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"{' '.join(args)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
        return result

    def git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self.run("git", *args, check=check)

    def write(self, rel: str, content: str | bytes) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target

    def commit(
        self, files: dict[str, str | bytes], message: str, *, force_add: bool = False
    ) -> subprocess.CompletedProcess[str]:
        for rel, content in files.items():
            self.write(rel, content)
        add = ["add", "-f"] if force_add else ["add"]
        self.git(*add, *files, check=True)
        msg_file = self.path.parent / "commit-message.txt"
        msg_file.write_text(message, encoding="utf-8")
        return self.git("commit", "-F", str(msg_file))

    def head_count(self) -> int:
        out = self.git("rev-list", "--count", "HEAD")
        return int(out.stdout.strip()) if out.returncode == 0 else 0

    def script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one of our guard scripts the way `make check` does."""
        return self.run(
            "uv", "run", "--frozen", "--project", "backend", "python", f"scripts/{name}", *args
        )
