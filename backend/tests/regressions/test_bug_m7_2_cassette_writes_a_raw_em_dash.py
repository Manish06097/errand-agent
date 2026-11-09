"""BUG-M7-2: a recorded cassette fails the tracked-files em-dash guard when Opus writes one.

`_CassetteWriter` dumped JSON with `ensure_ascii=False`, so a model answer containing
U+2014 landed in the committed cassette as the raw character, and `make check`'s
em-dash guard (which rejects the character in any tracked text file) failed on a file
nobody may edit by hand.

End to end: record through a fake transport whose answer has an em dash, then run the
guard on the cassette. The decoded data must be identical either way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

from errand.llm.cassette import load_cassette
from errand.llm.scripted import ScriptedChatModel
from tests.hygiene.repo import REPO_ROOT
from tests.llm.support import S4_USAGE, SYSTEM, rig, text

DASH = "\u2014"


async def test_a_recorded_em_dash_does_not_reach_the_cassette_file(tmp_path: Path) -> None:
    cassette = tmp_path / "cassettes" / "dash.jsonl"
    answer = f"Tap Masters {DASH} free on Tuesday at 09:00."
    upstream = ScriptedChatModel(script=[text(S4_USAGE[1], answer)])
    recording = rig(tmp_path / "rec", mode="record", transport=upstream, cassette=cassette)
    session = recording.gateway.session(system=SYSTEM, messages=[HumanMessage("Find me a plumber")])
    await session.call()

    # The recording is complete and the dash survives a round trip through the file.
    (entry,) = load_cassette(cassette)
    assert entry.response["content"][0]["text"] == answer
    guard = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_em_dash.py"), str(cassette)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert guard.returncode == 0, guard.stderr
