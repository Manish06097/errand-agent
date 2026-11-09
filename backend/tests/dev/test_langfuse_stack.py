"""infra/langfuse and `make langfuse-up`: pinned, private by default, secrets generated.

The compose checks render the real merged config with `docker compose config`, which
needs the docker CLI but no running daemon; they are skipped where docker is absent.
Bringing the stack up is the live A5 check, not part of this suite.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.hygiene.repo import REPO_ROOT

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check

INFRA = REPO_ROOT / "infra" / "langfuse"
UPSTREAM = REPO_ROOT / "spikes" / "langfuse" / "downloaded" / "docker-compose.yml"
# spikes/README.md: upstream commit 05ece6fd, downloaded 2026-11-06.
UPSTREAM_SHA256 = "cf67837af1f69e6a0d82125bd623e63401b1b8876bf467c8c22adb8c384c7ff2"
CHANGEME_DEFAULTS = {
    "SALT": "mysalt",
    "NEXTAUTH_SECRET": "mysecret",
    "ENCRYPTION_KEY": "0" * 64,
    "CLICKHOUSE_PASSWORD": "clickhouse",
    "REDIS_AUTH": "myredissecret",
    "MINIO_ROOT_PASSWORD": "miniosecret",
    "POSTGRES_PASSWORD": "postgres",
}
# Exact releases: what upstream's floating tags (langfuse:4, redis:7, postgres:17) pointed
# to on 2026-11-06. Postgres releases are major.minor; chainguard's minio has only
# `latest`, so it is pinned by digest.
IMAGES = {
    "langfuse-web": "docker.langfuse.com/langfuse/langfuse:4.76.0",
    "langfuse-worker": "docker.langfuse.com/langfuse/langfuse-worker:4.76.0",
    "clickhouse": "docker.io/clickhouse/clickhouse-server:26.3.62.3",
    "minio": "cgr.dev/chainguard/minio@sha256:"
    "aea6c163749e1caca37f907976dcf337177ddf5f558808f2aa591618fd3a6eef",
    "redis": "docker.io/redis:7.4.12",
    "postgres": "docker.io/postgres:17.11",
}


def load_script(name: str) -> ModuleType:
    qualified = f"errand_scripts_{name}"
    if qualified in sys.modules:
        return sys.modules[qualified]
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(qualified, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before running it: dataclasses look their module up in sys.modules.
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


STACK = ("uv", "run", "--frozen", "--project", "backend", "python", "scripts/langfuse_stack.py")


def run_stack(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*STACK, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_compose_file_is_the_upstream_one_byte_for_byte() -> None:
    data = (INFRA / "docker-compose.yml").read_bytes()
    assert hashlib.sha256(data).hexdigest() == UPSTREAM_SHA256
    assert data == UPSTREAM.read_bytes()


def test_the_env_file_is_generated_once_with_fresh_secrets(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    result = run_stack("env", "--path", str(path))
    assert result.returncode == 0, result.stderr
    stack = load_script("langfuse_stack")
    values = stack.read_env(path)
    assert set(values) == set(stack.env_values())
    for name, default in CHANGEME_DEFAULTS.items():
        assert values[name] != default and len(values[name]) >= 32, name
    assert re.fullmatch(r"[0-9a-f]{64}", values["ENCRYPTION_KEY"])
    assert re.fullmatch(r"pk-lf-[0-9a-f-]{36}", values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
    assert re.fullmatch(r"sk-lf-[0-9a-f-]{36}", values["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
    assert path.stat().st_mode & 0o077 == 0
    # A second run keeps the file (and so the stack's credentials).
    before = path.read_text()
    assert run_stack("env", "--path", str(path)).returncode == 0
    assert path.read_text() == before
    # A fresh file gets different secrets.
    other = tmp_path / "other.env"
    assert run_stack("env", "--path", str(other)).returncode == 0
    assert stack.read_env(other)["SALT"] != values["SALT"]


def test_the_real_env_file_is_gitignored_and_the_template_is_not() -> None:
    def ignored(path: Path) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(path)], cwd=REPO_ROOT, capture_output=True
        )
        return result.returncode == 0

    assert ignored(INFRA / ".env")
    assert not ignored(INFRA / ".env.example")


def test_the_template_has_every_variable_and_no_key_shaped_placeholder() -> None:
    stack = load_script("langfuse_stack")
    template = stack.read_env(INFRA / ".env.example")
    assert set(template) == set(stack.env_values())
    scan = subprocess.run(
        [
            *("uv", "run", "--frozen", "--project", "backend", "python"),
            *("scripts/secret_scan.py", str(INFRA / ".env.example")),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert scan.returncode == 0, scan.stdout + scan.stderr


def test_langfuse_up_without_docker_fails_clearly() -> None:
    env = {**os.environ, "DOCKER": "errand-no-such-docker"}
    result = subprocess.run(
        ["make", "--no-print-directory", "langfuse-up"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert "docker not found" in result.stderr
    assert "do not need it" in result.stderr


def test_docker_memory_sizes_parse_for_the_low_memory_warning() -> None:
    stack = load_script("langfuse_stack")
    assert stack._bytes("1.5GiB") == 1.5 * 1024**3
    assert stack._bytes("512MiB ") == 512 * 1024**2
    assert stack._bytes("12kB") == 12_000
    assert stack._bytes("--") == 0.0


@pytest.fixture
def merged_config(tmp_path: Path) -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    env_file = tmp_path / ".env"
    assert run_stack("env", "--path", str(env_file)).returncode == 0
    stack = load_script("langfuse_stack")
    result = subprocess.run(
        stack.compose(env_file, "config", "--format", "json"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    config["_env"] = stack.read_env(env_file)
    return config


def test_only_langfuse_web_is_published_and_only_on_loopback(merged_config: dict[str, Any]) -> None:
    published = {name: svc.get("ports") or [] for name, svc in merged_config["services"].items()}
    assert {name for name, ports in published.items() if ports} == {"langfuse-web"}
    assert published["langfuse-web"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 3000,
            "published": "3100",
            "protocol": "tcp",
        }
    ]
    # The AI gateway only exists under its profile.
    assert "langfuse-ai-gateway" not in merged_config["services"]


def test_every_image_is_pinned_and_telemetry_is_off(merged_config: dict[str, Any]) -> None:
    services = merged_config["services"]
    assert {name: svc["image"] for name, svc in services.items()} == IMAGES
    for name in ("langfuse-web", "langfuse-worker"):
        env = services[name]["environment"]
        assert env["TELEMETRY_ENABLED"] == "false"
        assert env["NEXTAUTH_URL"] == "http://localhost:3100"


def test_no_upstream_changeme_default_survives(merged_config: dict[str, Any]) -> None:
    services = merged_config["services"]
    values = merged_config["_env"]
    web = services["langfuse-web"]["environment"]
    for name in ("SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET", "CLICKHOUSE_PASSWORD", "REDIS_AUTH"):
        assert web[name] == values[name] and web[name] != CHANGEME_DEFAULTS[name], name
    assert web["DATABASE_URL"] == (
        f"postgresql://postgres:{values['POSTGRES_PASSWORD']}@postgres:5432/postgres"
    )
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == values["POSTGRES_PASSWORD"]
    assert services["minio"]["environment"]["MINIO_ROOT_PASSWORD"] == values["MINIO_ROOT_PASSWORD"]
    assert web["LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY"] == values["MINIO_ROOT_PASSWORD"]
    assert values["REDIS_AUTH"] in " ".join(services["redis"]["command"])
    for name in (
        "LANGFUSE_INIT_ORG_ID",
        "LANGFUSE_INIT_PROJECT_ID",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY",
        "LANGFUSE_INIT_USER_EMAIL",
        "LANGFUSE_INIT_USER_PASSWORD",
    ):
        assert web[name] == values[name], name


def test_compose_refuses_to_run_without_the_generated_env() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    stack = load_script("langfuse_stack")
    result = subprocess.run(
        stack.compose(Path(os.devnull), "config"), capture_output=True, text=True, timeout=60
    )
    assert result.returncode != 0
    assert "run make langfuse-up" in result.stderr


@pytest.mark.parametrize(
    ("errand_langfuse", "env_file", "exported"),
    [(None, True, True), ("off", True, False), (None, False, False)],
    ids=["env_file", "off", "no_env_file"],
)
def test_make_dev_exports_the_keys_only_when_the_stack_exists_and_langfuse_is_on(
    tmp_path: Path, errand_langfuse: str | None, env_file: bool, exported: bool
) -> None:
    dev = load_script("dev")
    path = tmp_path / ".env"
    if env_file:
        assert run_stack("env", "--path", str(path)).returncode == 0
    env = {} if errand_langfuse is None else {"ERRAND_LANGFUSE": errand_langfuse}
    extra = dev.langfuse_env(env, path)
    if not exported:
        assert extra == {}
        return
    values = load_script("langfuse_stack").read_env(path)
    assert extra == {
        "LANGFUSE_PUBLIC_KEY": values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
        "LANGFUSE_SECRET_KEY": values["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
        "LANGFUSE_HOST": "http://localhost:3100",
    }


def test_the_ai_gateway_profile_is_pinned_and_unpublished(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    env_file = tmp_path / ".env"
    assert run_stack("env", "--path", str(env_file)).returncode == 0
    stack = load_script("langfuse_stack")
    command = stack.compose(env_file, "--profile", "ai-gateway", "config", "--format", "json")
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    gateway = json.loads(result.stdout)["services"]["langfuse-ai-gateway"]
    assert gateway["image"] == "docker.langfuse.com/langfuse/ai-gateway:4.76.0"
    assert not gateway.get("ports")


@pytest.mark.parametrize("imports_langfuse", [True, False])
def test_make_evals_fails_if_anything_imported_langfuse(
    tmp_path: Path, imports_langfuse: bool
) -> None:
    # The real evals conftest hook, run over a probe scenario in a throwaway evals dir.
    evals_dir = tmp_path / "evals_probe"
    evals_dir.mkdir()
    shutil.copy(REPO_ROOT / "backend" / "evals" / "conftest.py", evals_dir / "conftest.py")
    body = "import langfuse  # noqa: F401" if imports_langfuse else "pass"
    (evals_dir / "test_probe.py").write_text(f"def test_probe() -> None:\n    {body}\n")
    env = {**os.environ, "ERRAND_EVALS_DIR": str(evals_dir)}
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    result = subprocess.run(
        [*STACK[:-1], "scripts/run_evals.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if imports_langfuse:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "langfuse was imported during the eval run" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
