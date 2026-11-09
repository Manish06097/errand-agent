"""BUG-M4-1: filling the Approval section turned `make check` red a second time.

`tests/hygiene/test_commit_hooks.py::test_over_budget_design_note_is_rejected` committed
the real docs/DESIGN_NOTE.md and then added 91 words to its Approval section, expecting
"Approval: 91 words, over its budget of 90". Once M4 filled Approval (85 words) the hook
rightly reported 176 and the test failed, although the note is valid. It is BUG-M2-1 again,
in the one hygiene test that BUG-M2-1's fix did not cover.

The repro runs that test with the design note replaced by one whose Approval section is
filled to its budget, the way a finished milestone leaves it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.hygiene import test_commit_hooks
from tests.hygiene.conftest import _hooked_template, _pre_commit_home, hooked_repo  # noqa: F401
from tests.hygiene.repo import CLEAN_DESIGN_NOTE, HookedRepo

pytestmark = pytest.mark.slow  # subprocesses or real servers: serial step of make check


def _with_approval_filled(note: str) -> str:
    heading = "## Approval\n<!-- budget: 90 words -->\n"
    assert heading in note
    after = note.split(heading, 1)[1]
    rest = after[after.index("\n## ") :] if "\n## " in after else ""
    return note.split(heading, 1)[0] + heading + " ".join(["word"] * 85) + "\n" + rest


def test_over_budget_hook_test_passes_with_a_filled_approval_section(
    hooked_repo: HookedRepo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filled = tmp_path / "DESIGN_NOTE.md"
    filled.write_text(
        _with_approval_filled(CLEAN_DESIGN_NOTE.read_text(encoding="utf-8")), encoding="utf-8"
    )
    monkeypatch.setattr(test_commit_hooks, "CLEAN_DESIGN_NOTE", filled)
    test_commit_hooks.test_over_budget_design_note_is_rejected(hooked_repo)
