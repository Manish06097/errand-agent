"""The masked trace reader fails closed (review round 1, gate 2 decision 14)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from errand.memory import hmac_key
from errand.memory.mask import hash_of
from errand.trace.cli import show
from errand.trace.masked import MaskUnavailable, masker_for


def _database(data_dir: Path, with_hash: bool, key: bytes) -> None:
    conn = sqlite3.connect(data_dir / "agent.sqlite")
    conn.execute(
        "CREATE TABLE memory_surface_hashes (hmac TEXT PRIMARY KEY, memory_id TEXT NOT NULL, "
        "kind TEXT NOT NULL)"
    )
    if with_hash:
        digest = hash_of(key, "Indiranagar")
        conn.execute(
            "INSERT INTO memory_surface_hashes VALUES (?, 'm-01', 'surface_form')", (digest,)
        )
    conn.commit()
    conn.close()


def test_no_database_masks_nothing(tmp_path: Path) -> None:
    assert not masker_for(tmp_path).active


def test_a_database_without_hashes_masks_nothing_even_with_no_key(tmp_path: Path) -> None:
    _database(tmp_path, with_hash=False, key=b"")
    assert not masker_for(tmp_path).active


def test_a_database_that_predates_the_memory_tables_masks_nothing(tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "agent.sqlite").close()
    assert not masker_for(tmp_path).active


@pytest.mark.parametrize(
    "write_key",
    [
        None,  # no key file at all
        lambda p: p.write_text("not hex", encoding="ascii"),
        lambda p: p.write_text("ab" * 16, encoding="ascii"),  # 16 bytes, not 32
        lambda p: p.write_text("", encoding="ascii"),
    ],
    ids=["missing", "malformed", "wrong-size", "empty"],
)
def test_hashes_with_an_unusable_key_refuse_the_read(tmp_path: Path, write_key) -> None:  # type: ignore[no-untyped-def]
    _database(tmp_path, with_hash=True, key=hmac_key.fixed_key("t"))
    if write_key is not None:
        write_key(tmp_path / "memory_hmac.key")
    with pytest.raises(MaskUnavailable) as err:
        masker_for(tmp_path)
    assert "memory_hmac.key" in str(err.value)


def test_an_unreadable_database_refuses_the_read(tmp_path: Path) -> None:
    (tmp_path / "agent.sqlite").write_bytes(b"this is not a sqlite file at all" * 8)
    with pytest.raises(MaskUnavailable):
        masker_for(tmp_path)


def test_a_good_key_and_hashes_give_an_active_masker(tmp_path: Path) -> None:
    key = hmac_key.fixed_key("t")
    _database(tmp_path, with_hash=True, key=key)
    (tmp_path / "memory_hmac.key").write_text(key.hex(), encoding="ascii")
    masker = masker_for(tmp_path)
    assert masker.active and "Indiranagar" not in masker.text("a plumber in Indiranagar")


def test_errand_trace_show_says_why_and_prints_nothing_when_masking_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _database(tmp_path, with_hash=True, key=hmac_key.fixed_key("t"))  # and no key file
    traces = tmp_path / "traces" / "2026"
    traces.mkdir(parents=True)
    trace = traces / "r1.jsonl"
    trace.write_text('{"type": "run.start", "seq": 0, "run_id": "r1"}\n', encoding="utf-8")
    assert show(str(trace), tmp_path) == 3
    out = capsys.readouterr()
    assert out.out == "" and "memory_hmac.key" in out.err
