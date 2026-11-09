"""Fixtures for the hygiene tests: a fresh hooked repo per test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.hygiene.repo import REPO_ROOT, HookedRepo, isolated_env


@pytest.fixture(scope="session")
def _pre_commit_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("home")


@pytest.fixture(scope="session")
def _hooked_template(tmp_path_factory: pytest.TempPathFactory, _pre_commit_home: Path) -> Path:
    """The hooked repo, built once per session: git init, the plain `pre-commit install`
    that `make setup` runs, and one real commit through the hooks. Each test copies it
    (about a second of setup saved per test, 40 tests)."""
    path = tmp_path_factory.mktemp("hooked-template") / "repo"
    path.mkdir()
    repo = HookedRepo(path=path, env=isolated_env(_pre_commit_home))
    repo.git("init", "-q", check=True)
    shutil.copy2(REPO_ROOT / ".pre-commit-config.yaml", path / ".pre-commit-config.yaml")
    (path / "backend").symlink_to(REPO_ROOT / "backend", target_is_directory=True)
    (path / "scripts").symlink_to(REPO_ROOT / "scripts", target_is_directory=True)
    # The symlinks are plumbing, not content under test.
    (path / ".git" / "info" / "exclude").write_text("backend\nscripts\n")
    # Plain install, no -t flags: exactly what `make setup` does. If the config lost
    # default_install_hook_types, the commit-msg hook would silently not exist.
    repo.run("uv", "run", "--frozen", "--project", "backend", "pre-commit", "install", check=True)
    repo.git("add", ".pre-commit-config.yaml", check=True)
    first = repo.git("commit", "-q", "-m", "Add hook config")
    assert first.returncode == 0, first.stdout + first.stderr
    return path


@pytest.fixture
def hooked_repo(
    tmp_path: Path, _pre_commit_home: Path, _hooked_template: Path
) -> Iterator[HookedRepo]:
    path = tmp_path / "repo"
    shutil.copytree(_hooked_template, path, symlinks=True)
    yield HookedRepo(path=path, env=isolated_env(_pre_commit_home))
