"""The two seeds: deterministic, the mix of decision 23, and canaries where the manifest
says (decision 24). Each seed is built once per module."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from errand.seed.base import dumps_manifest
from errand.seed.memory_demo import build_demo
from errand.seed.memory_rich import TOTAL_MESSAGES, build_rich
from errand.store.agent_db import AgentDB

USER = "u_demo"


async def _build_rich(path: Path) -> tuple[str, dict[str, Any]]:
    seeded, manifest = await build_rich(path)
    async with seeded.db.session() as repos:
        dump = "\n".join([line async for line in repos.conn.iterdump()])
    await seeded.db.close()
    return dump, manifest


@pytest.fixture(scope="module")
def rich(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any], str]:
    path = tmp_path_factory.mktemp("rich-a")
    dump, manifest = asyncio.run(_build_rich(path))
    return path, manifest, dump


@pytest.fixture(scope="module")
def rich_again(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any], str]:
    path = tmp_path_factory.mktemp("rich-b")
    dump, manifest = asyncio.run(_build_rich(path))
    return path, manifest, dump


def _query(path: Path, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{path / 'agent.sqlite'}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_two_builds_are_byte_for_byte_equal(
    rich: tuple[Path, dict[str, Any], str], rich_again: tuple[Path, dict[str, Any], str]
) -> None:
    _, manifest_a, dump_a = rich
    _, manifest_b, dump_b = rich_again
    assert dump_a == dump_b
    assert dumps_manifest(manifest_a) == dumps_manifest(manifest_b)
    assert (rich[0] / "memory_hmac.key").read_text() == (
        rich_again[0] / "memory_hmac.key"
    ).read_text()


def test_the_mix_is_decision_23_with_fixed_ids(rich: tuple[Path, dict[str, Any], str]) -> None:
    _, manifest, _ = rich
    rows = manifest["rows"]
    assert [r["id"] for r in rows] == [f"m-{i:02d}" for i in range(1, 61)]
    assert manifest["counts"] == {
        "active": 32,  # 20 explicit + 4 inferred confirmed + 8 irrelevant
        "pending_confirmation": 9,
        "superseded": 8,
        "forgotten": 7,
        "rejected": 4,
    }
    active = [r for r in rows if r["status"] == "active"]
    irrelevant = [
        r for r in active if r["scope"] in ("service:pest_control", "service:painting")
    ] + [r for r in active if r["key"] == "contact_window"]
    assert len(irrelevant) == 8
    assert sum(1 for r in active if r["kind"] == "inferred") == 4
    assert sum(1 for r in active if r["kind"] == "explicit") - 8 == 20
    rejected = sorted(r["rejected_because"] for r in rows if r["status"] == "rejected")
    assert rejected == ["dismissed", "dismissed", "superseded_by_explicit", "undone"]
    assert all(r["kind"] == "inferred" for r in rows if r["status"] == "pending_confirmation")
    assert manifest["messages"] == TOTAL_MESSAGES


def test_the_three_version_home_area_chain_and_the_conflicts_on_purpose(
    rich: tuple[Path, dict[str, Any], str],
) -> None:
    _, manifest, _ = rich
    chain = [r for r in manifest["rows"] if r["key"] == "home_area"]
    assert [r["status"] for r in chain] == ["superseded", "superseded", "active"]
    scopes = {(r["key"], r["scope"]) for r in manifest["rows"] if r["status"] == "active"}
    assert ("earliest_time", "global") in scopes
    assert ("earliest_time", "service:electrician") in scopes
    # the rule must not suggest P7 against the explicit plumber avoid
    assert manifest["inference_declined"] == [
        {"scope": "service:plumber", "value": "P7", "why": "active_row_exists"}
    ]
    # a pending inference rejected by a later explicit write
    sup = next(r for r in manifest["rows"] if r["rejected_because"] == "superseded_by_explicit")
    assert (sup["key"], sup["scope"]) == ("preferred_provider", "service:ac_servicing")


def test_pending_and_dismissed_rows_are_typed_with_booking_evidence_only(
    rich: tuple[Path, dict[str, Any], str],
) -> None:
    path, manifest, _ = rich
    inferred_ids = [r["id"] for r in manifest["rows"] if r["kind"] == "inferred"]
    assert len(inferred_ids) == 16
    for memory_id in inferred_ids:
        (evidence,) = _query(
            path, "SELECT evidence_json FROM memories WHERE memory_id = ?", memory_id
        )[0]
        doc = json.loads(evidence)
        assert doc["type"] == "rule" and doc["booking_ids"] and "quote" not in doc


def test_forgotten_rows_hold_no_value_and_no_forgotten_canary_is_left(
    rich: tuple[Path, dict[str, Any], str],
) -> None:
    path, manifest, dump = rich
    values = _query(path, "SELECT value_json FROM memories WHERE status = 'forgotten'")
    assert len(values) == 7 and all(v == (None,) for v in values)
    assert "CANARY-FORGOTTEN" not in dump
    assert _query(path, "SELECT COUNT(*) FROM memory_blocklist WHERE reason = 'forgotten'") == [
        (7,)
    ]
    assert _query(path, "SELECT COUNT(*) FROM memory_surface_hashes")[0][0] > 7
    forgotten = [c for c, d in manifest["canaries"].items() if d["kind"] == "forgotten"]
    assert len(forgotten) == 7


def test_each_canary_is_unique_and_planted_where_the_manifest_says(
    rich: tuple[Path, dict[str, Any], str],
) -> None:
    path, manifest, _ = rich
    canaries = manifest["canaries"]
    assert len(canaries) == len(set(canaries))
    for canary, doc in canaries.items():
        for place in doc["places"]:
            if place["kind"] == "message":
                (body,) = _query(
                    path, "SELECT body_json FROM messages WHERE message_id = ?", place["id"]
                )[0]
            elif place["kind"] == "errand":
                (body,) = _query(
                    path, "SELECT brief_json FROM errands WHERE errand_id = ?", place["id"]
                )[0]
            else:
                (body,) = _query(
                    path,
                    "SELECT origin_brief_json FROM known_bookings WHERE booking_id = ?",
                    place["id"],
                )[0]
            if doc["kind"] == "forgotten":
                # planted, then removed by the real forget
                assert canary not in body, (canary, place)
                if place["kind"] == "message":
                    assert "[removed at your request]" in body, (canary, place)
            else:
                assert canary in body, (canary, place)
    # the user's own words for the undone row stay (decision 5)
    (kept,) = [c for c, d in canaries.items() if d["kind"] == "undone"]
    assert kept == "CANARY-UNDONE-5521" and manifest["user_words_kept"]
    note = manifest["user_words_kept"][0]
    assert note["message_id"] in {p["id"] for p in canaries[kept]["places"]}


def test_some_canaries_sit_inside_the_last_six_turns(
    rich: tuple[Path, dict[str, Any], str],
) -> None:
    path, manifest, _ = rich
    rows = _query(
        path, "SELECT message_id, role, body_json FROM messages ORDER BY created_at, rowid"
    )
    turns: list[list[tuple[str, str]]] = []
    for message_id, role, body in rows:
        if role == "user" or not turns:
            turns.append([])
        turns[-1].append((message_id, body))
    window = {mid for turn in turns[-6:] for mid, _ in turn}
    placed = {
        (c, p["id"])
        for c, d in manifest["canaries"].items()
        for p in d["places"]
        if p["kind"] == "message"
    }
    assert {c for c, mid in placed if mid in window and c.startswith("CANARY-SUPERSEDED")}
    assert {c for c, mid in placed if mid in window and c.startswith("CANARY-FORGOTTEN")}


def test_the_thread_bookings_and_archives(rich: tuple[Path, dict[str, Any], str]) -> None:
    path, _, _ = rich
    assert _query(path, "SELECT COUNT(*) FROM errands") == [(6,)]
    assert (
        _query(path, "SELECT COUNT(*) FROM known_bookings WHERE status = 'confirmed'")[0][0] >= 30
    )
    assert _query(path, "SELECT COUNT(*) FROM known_bookings WHERE provider_id = 'E4'") == [(1,)]
    assert _query(path, "SELECT COUNT(*) FROM known_bookings WHERE provider_id = 'A2'")[0][0] >= 3
    (sessions,) = _query(
        path, "SELECT COUNT(DISTINCT date(created_at / 1000 + 19800, 'unixepoch')) FROM messages"
    )[0]
    assert sessions == 6


async def test_the_demo_seed_has_eight_realistic_rows_and_no_canary(tmp_path: Path) -> None:
    seeded = await build_demo(tmp_path)
    async with seeded.db.session() as repos:
        rows = await repos.memories.for_user(USER)
        dump = "\n".join([line async for line in repos.conn.iterdump()])
    await seeded.db.close()
    assert len(rows) == 8 and {r.status for r in rows} == {"active"}
    pairs = {(r.key, r.scope): r.value for r in rows}
    assert pairs[("home_area", "global")] == "indiranagar"
    assert pairs[("earliest_time", "global")] == "08:00"
    assert pairs[("earliest_time", "service:electrician")] == "10:00"
    assert ("budget_ceiling", "service:electrician") in pairs
    assert ("avoid_provider", "service:plumber") in pairs
    assert {"language", "access_note", "contact_window"} <= {r.key for r in rows}
    assert "CANARY" not in dump
    reopened = await AgentDB.open(tmp_path / "agent.sqlite")
    await reopened.close()
