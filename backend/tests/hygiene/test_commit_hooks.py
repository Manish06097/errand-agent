"""The hygiene rules hold at `git commit` time, through the installed hooks.

Every forbidden string here is assembled at runtime (chr(), concatenation) so this
file never contains a literal em dash, key or attribution line itself.
"""

from __future__ import annotations

import secrets
import string

import pytest

from tests.hygiene.repo import CLEAN_DESIGN_NOTE, HookedRepo
from tests.hygiene.test_design_note import skeleton

pytestmark = pytest.mark.slow  # subprocesses or real servers: second step of make check

EM_DASH = chr(0x2014)
ROBOT = chr(0x1F916)
VENDOR_ADDRESS = "noreply@" + "anthropic.com"


def fake_anthropic_key() -> str:
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(90))
    return "sk-" + "ant-" + "api03-" + body + "AA"


def fake_langfuse_key(kind: str) -> str:
    hexd = secrets.token_hex(16)
    uuid = f"{hexd[:8]}-{hexd[8:12]}-{hexd[12:16]}-{hexd[16:20]}-{hexd[20:]}"
    return f"{kind}-" + "lf-" + uuid


def assert_rejected(repo: HookedRepo, result_code: int, before: int) -> None:
    assert result_code != 0
    assert repo.head_count() == before, "a rejected commit must not be recorded"


def test_plain_install_wires_both_hook_stages(hooked_repo: HookedRepo) -> None:
    hooks = hooked_repo.path / ".git" / "hooks"
    assert (hooks / "pre-commit").is_file()
    assert (hooks / "commit-msg").is_file(), (
        "pre-commit install did not set up commit-msg; the attribution guard would never run"
    )


def test_clean_commit_succeeds(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    result = hooked_repo.commit(
        {
            "notes/plain.md": "A plain note - with a hyphen.\n",
            "docs/DESIGN_NOTE.md": CLEAN_DESIGN_NOTE.read_text(encoding="utf-8"),
        },
        "Add a plain note\n\nBody text that names no tool.\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert hooked_repo.head_count() == before + 1
    # The hooks really ran, rather than being skipped.
    for hook in ("em-dash guard", "secret scan", "design note word budget", "attribution guard"):
        assert hook in result.stdout + result.stderr


def test_staged_em_dash_is_rejected(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    result = hooked_repo.commit({"notes/dash.md": f"one{EM_DASH}two\n"}, "Add a note\n")
    assert_rejected(hooked_repo, result.returncode, before)
    assert "notes/dash.md:1:4: U+2014 em dash" in result.stderr + result.stdout


def test_em_dash_in_a_binary_file_is_ignored(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    blob = b"\x89PNG\r\n\x1a\n\0\0" + EM_DASH.encode("utf-8")
    result = hooked_repo.commit({"assets/img.png": blob}, "Add an image\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert hooked_repo.head_count() == before + 1


@pytest.mark.parametrize(
    "message",
    [
        "Add feature\n\nCo-Authored-By: Claude <" + VENDOR_ADDRESS + ">\n",
        "Add feature\n\nco-authored-by: GitHub Copilot <copilot@example.invalid>\n",
        "Add feature\n\nAssisted-by: ChatGPT\n",
        "Add feature\n\n" + ROBOT + " Generated with [Some Tool](https://example.invalid)\n",
        "Add feature\n\nGenerated with an assistant\n",
        "Add feature " + ROBOT + "\n",
        "Add feature\n\nContact " + VENDOR_ADDRESS + "\n",
    ],
    ids=[
        "claude-trailer",
        "copilot-trailer",
        "assisted-by",
        "generated-with-link",
        "generated-with",
        "robot-emoji",
        "vendor-address",
    ],
)
def test_ai_attribution_in_message_is_rejected(hooked_repo: HookedRepo, message: str) -> None:
    before = hooked_repo.head_count()
    result = hooked_repo.commit({"notes/ok.md": "fine\n"}, message)
    assert_rejected(hooked_repo, result.returncode, before)
    assert "attribution guard" in result.stdout + result.stderr


def test_human_co_author_is_allowed(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    message = "Pair on the parser\n\nCo-Authored-By: Asha Rao <asha@example.invalid>\n"
    result = hooked_repo.commit({"notes/pair.md": "paired\n"}, message)
    assert result.returncode == 0, result.stdout + result.stderr
    assert hooked_repo.head_count() == before + 1


def test_anthropic_key_is_rejected(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    key = fake_anthropic_key()
    result = hooked_repo.commit({"src/settings.py": f'API_KEY = "{key}"\n'}, "Add settings\n")
    assert_rejected(hooked_repo, result.returncode, before)
    output = result.stdout + result.stderr
    assert "src/settings.py:1: anthropic-api-key" in output
    assert key not in output, "the scanner must never echo a matched secret"


@pytest.mark.parametrize("kind", ["sk", "pk"])
def test_langfuse_key_is_rejected(hooked_repo: HookedRepo, kind: str) -> None:
    before = hooked_repo.head_count()
    key = fake_langfuse_key(kind)
    result = hooked_repo.commit({"infra/keys.env.txt": f"KEY={key}\n"}, "Add keys\n")
    assert_rejected(hooked_repo, result.returncode, before)
    assert key not in result.stdout + result.stderr


def test_compose_defaults_are_not_flagged(hooked_repo: HookedRepo) -> None:
    """Upstream CHANGEME placeholders and short dummy keys are not credentials."""
    before = hooked_repo.head_count()
    compose = (
        "SALT: ${SALT:-mysalt} # CHANGEME\n"
        "ENCRYPTION_KEY: ${ENCRYPTION_KEY:-" + "0" * 64 + "} # CHANGEME\n"
        'public_key = "pk-' + 'lf-x"\n'
    )
    result = hooked_repo.commit({"infra/docker-compose.yml": compose}, "Add compose\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert hooked_repo.head_count() == before + 1


def test_tracked_env_file_is_rejected(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    result = hooked_repo.commit({".env": "DEBUG=1\n"}, "Add env\n", force_add=True)
    assert_rejected(hooked_repo, result.returncode, before)
    assert ".env: .env files must never be committed" in result.stdout + result.stderr


def test_env_example_is_allowed(hooked_repo: HookedRepo) -> None:
    before = hooked_repo.head_count()
    result = hooked_repo.commit({".env.example": "ANTHROPIC_API_KEY=\n"}, "Add env example\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert hooked_repo.head_count() == before + 1


def test_local_env_key_value_is_rejected_anywhere(hooked_repo: HookedRepo) -> None:
    """The real key from the gitignored .env, even in a shape no pattern knows."""
    value = "local-" + secrets.token_urlsafe(32)
    hooked_repo.write(".env", f'ANTHROPIC_API_KEY="{value}"\n')
    before = hooked_repo.head_count()
    result = hooked_repo.commit({"notes/paste.md": f"oops {value} pasted\n"}, "Add paste\n")
    assert_rejected(hooked_repo, result.returncode, before)
    output = result.stdout + result.stderr
    assert "contains the value of ANTHROPIC_API_KEY from the local .env" in output
    assert value not in output


def _over_budget(note: str, section: str, extra_words: int) -> str:
    heading = f"## {section}\n"
    assert heading in note
    filler = " ".join(["word"] * extra_words)
    return note.replace(heading, heading + filler + "\n", 1)


def test_over_budget_design_note_is_rejected(hooked_repo: HookedRepo) -> None:
    # The structure of the real note, none of its prose: it fills up milestone by milestone
    # (BUG-M2-1, BUG-M4-1), and this test adds words to Approval and counts them.
    note = skeleton(CLEAN_DESIGN_NOTE.read_text(encoding="utf-8"))
    ok = hooked_repo.commit({"docs/DESIGN_NOTE.md": note}, "Add design note\n")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    before = hooked_repo.head_count()
    result = hooked_repo.commit(
        {"docs/DESIGN_NOTE.md": _over_budget(note, "Approval", 91)}, "Expand approval\n"
    )
    assert_rejected(hooked_repo, result.returncode, before)
    assert "Approval: 91 words, over its budget of 90" in result.stdout + result.stderr


def test_make_check_history_scan_catches_a_bypassed_commit(hooked_repo: HookedRepo) -> None:
    """`--no-verify` skips the hooks; the history scan in make check still fails."""
    assert hooked_repo.script("check_attribution.py", "--history").returncode == 0
    hooked_repo.write("notes/x.md", "x\n")
    hooked_repo.git("add", "notes/x.md", check=True)
    message = "Sneak\n\nCo-Authored-By: Claude <" + VENDOR_ADDRESS + ">\n"
    hooked_repo.git("commit", "-q", "--no-verify", "-m", message, check=True)
    result = hooked_repo.script("check_attribution.py", "--history")
    assert result.returncode == 1
    assert "Co-Authored-By trailer names an AI tool" in result.stderr


def test_make_check_tracked_scans_catch_bypassed_files(hooked_repo: HookedRepo) -> None:
    hooked_repo.write("notes/dash.md", f"a{EM_DASH}b\n")
    hooked_repo.write("notes/key.txt", fake_anthropic_key() + "\n")
    hooked_repo.git("add", "notes", check=True)
    hooked_repo.git("commit", "-q", "--no-verify", "-m", "Sneak files", check=True)
    dash = hooked_repo.script("check_em_dash.py", "--tracked")
    assert dash.returncode == 1
    assert "notes/dash.md:1:2" in dash.stderr
    scan = hooked_repo.script("secret_scan.py", "--tracked")
    assert scan.returncode == 1
    assert "notes/key.txt:1: anthropic-api-key" in scan.stderr
