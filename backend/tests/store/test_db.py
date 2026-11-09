"""agent.sqlite: migrations, per-connection pragmas, constraints and BEGIN IMMEDIATE."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from errand.store.db import connect, load_migrations, migrate, open_agent_db, transaction_immediate

TABLES_4_1 = {
    "users",
    "conversations",
    "messages",
    "message_redactions",
    "runs",
    "errands",
    "holds",
    "approvals",
    "action_intents",
    "intent_attempts",
    "known_bookings",
    "memories",
    "memory_events",
    "memory_blocklist",
    "notifications",
    "followup_jobs",
    "pending_resumes",
    "leases",
    "reader_cache",
    "reader_refusals",
    "client_requests",
    "schema_version",
}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await open_agent_db(tmp_path / "agent.sqlite")
    yield conn
    await conn.close()


async def _rows(conn: aiosqlite.Connection, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    async with conn.execute(sql, params) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_fresh_migrate_creates_every_table_and_a_rerun_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite"
    conn = await connect(path)
    assert await migrate(conn) == [m.version for m in load_migrations()] == [1, 2, 3, 4]
    names = {r[0] for r in await _rows(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == TABLES_4_1
    views = await _rows(conn, "SELECT name FROM sqlite_master WHERE type = 'view'")
    assert views == [("memories_eligible",)]
    schema_before = await _rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    assert await migrate(conn) == []
    await conn.close()

    again = await open_agent_db(path)
    assert await migrate(again) == []
    assert await _rows(again, "SELECT version FROM schema_version") == [(1,), (2,), (3,), (4,)]
    assert (
        await _rows(again, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
        == schema_before
    )
    await again.close()


async def test_two_processes_migrating_at_once_apply_each_migration_once(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite"
    a, b = await connect(path), await connect(path)
    results = await asyncio.gather(migrate(a), migrate(b))
    assert sorted(results) == [[], [1, 2, 3, 4]]
    await a.close()
    await b.close()


async def test_pragmas_hold_on_every_new_connection(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite"
    await (await open_agent_db(path)).close()
    for _ in range(3):
        conn = await connect(path)
        assert await _rows(conn, "PRAGMA journal_mode") == [("wal",)]
        assert await _rows(conn, "PRAGMA synchronous") == [(2,)]  # FULL
        assert await _rows(conn, "PRAGMA busy_timeout") == [(5000,)]
        assert await _rows(conn, "PRAGMA foreign_keys") == [(1,)]
        assert conn.isolation_level is None
        await conn.close()


# ------------------------------------------------------------------- constraints


def _approval(approval_id: str, status: str, conversation: str = "c_1") -> tuple[Any, ...]:
    return (
        approval_id,
        conversation,
        "e_1",
        f"prop_{approval_id}",
        "book",
        "fp1_x",
        "{}",
        "{}",
        status,
        1_000,
    )


async def _insert_approval(conn: aiosqlite.Connection, *row: Any) -> None:
    await conn.execute(
        "INSERT INTO approvals (approval_id, conversation_id, errand_id, proposal_id, action, "
        "fingerprint, material_json, card_json, status, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        row,
    )


async def test_one_live_approval_per_conversation(db: aiosqlite.Connection) -> None:
    await _insert_approval(db, *_approval("ap_1", "pending"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_approval(db, *_approval("ap_2", "pending"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_approval(db, *_approval("ap_2", "granted"))
    await _insert_approval(db, *_approval("ap_3", "pending", conversation="c_2"))
    await db.execute("UPDATE approvals SET status = 'invalidated' WHERE approval_id = 'ap_1'")
    await _insert_approval(db, *_approval("ap_4", "granted"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await _insert_approval(db, *_approval("ap_5", "approved", conversation="c_9"))


async def _insert_intent(conn: aiosqlite.Connection, intent_id: str, **cols: Any) -> None:
    row = {
        "intent_id": intent_id,
        "approval_id": f"ap_{intent_id}",
        "fingerprint": "fp1_x",
        "action": "cancel",
        "state": "open",
        **cols,
    }
    names = ", ".join(row)
    await conn.execute(
        f"INSERT INTO action_intents ({names}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


async def test_one_live_cancel_or_reschedule_per_target_booking(db: aiosqlite.Connection) -> None:
    await _insert_intent(db, "i1", action="cancel", target_booking_id="B-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_intent(db, "i2", action="reschedule", target_booking_id="B-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_intent(
            db, "i3", action="cancel", target_booking_id="B-1", state="needs_attention"
        )
    # Not live, or not a cancel/reschedule: allowed.
    await _insert_intent(db, "i4", action="cancel", target_booking_id="B-1", state="failed")
    await _insert_intent(db, "i5", action="book", target_booking_id="B-1")
    await db.execute("UPDATE action_intents SET state = 'succeeded' WHERE intent_id = 'i1'")
    await _insert_intent(db, "i6", action="reschedule", target_booking_id="B-1")


async def test_one_replacement_per_cancelled_booking_ever(db: aiosqlite.Connection) -> None:
    await _insert_intent(db, "r1", action="book", replaces_booking_id="B-9", state="succeeded")
    for state in ("open", "needs_attention", "succeeded"):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            await _insert_intent(
                db, f"r_{state}", action="book", replaces_booking_id="B-9", state=state
            )
    await _insert_intent(db, "r2", action="book", replaces_booking_id="B-9", state="failed")
    await _insert_intent(db, "r3", action="book", replaces_booking_id=None)
    await _insert_intent(db, "r4", action="book", replaces_booking_id=None)


async def test_one_intent_per_approval(db: aiosqlite.Connection) -> None:
    await _insert_intent(db, "a1", approval_id="ap_same")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_intent(db, "a2", approval_id="ap_same")


async def _insert_attempt(
    conn: aiosqlite.Connection, attempt_id: str, intent: str, n: int, key: str
) -> None:
    await conn.execute(
        "INSERT INTO intent_attempts "
        "(attempt_id, intent_id, n, idempotency_key, state, fence_epoch) "
        "VALUES (?, ?, ?, ?, 'prepared', 1)",
        (attempt_id, intent, n, key),
    )


async def test_attempt_keys_and_numbers_are_unique_and_need_an_intent(
    db: aiosqlite.Connection,
) -> None:
    await _insert_intent(db, "i1", action="book")
    await _insert_intent(db, "i2", action="book")
    await _insert_attempt(db, "at1", "i1", 1, "bo_aaa")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_attempt(db, "at2", "i2", 1, "bo_aaa")  # same key, other intent
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_attempt(db, "at3", "i1", 1, "bo_bbb")  # same (intent, n)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await _insert_attempt(db, "at4", "i_missing", 1, "bo_ccc")
    await _insert_attempt(db, "at5", "i1", 2, "bo_ddd")


async def _insert_memory(
    conn: aiosqlite.Connection, memory_id: str, status: str, scope: str = "global"
) -> None:
    await conn.execute(
        "INSERT INTO memories (memory_id, user_id, key, scope, value_json, value_hash, kind, "
        "status, created_at) VALUES (?, 'u_demo', 'home_area', ?, '\"Indiranagar\"', 'h', "
        "'explicit', ?, 1000)",
        (memory_id, scope, status),
    )


async def test_one_active_memory_per_key_and_scope(db: aiosqlite.Connection) -> None:
    await _insert_memory(db, "m1", "active")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await _insert_memory(db, "m2", "active")
    await _insert_memory(db, "m3", "active", scope="service:plumber")
    await _insert_memory(db, "m4", "pending_confirmation")
    await _insert_memory(db, "m5", "superseded")
    eligible = await _rows(db, "SELECT memory_id FROM memories_eligible ORDER BY memory_id")
    assert eligible == [("m1",), ("m3",)]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await _insert_memory(db, "m6", "maybe")
    # memory timestamps are integers like every other timestamp (section 4)
    assert await _rows(db, "SELECT typeof(created_at) FROM memories WHERE memory_id = 'm1'") == [
        ("integer",)
    ]


async def test_memory_events_are_append_only(db: aiosqlite.Connection) -> None:
    await _insert_memory(db, "m1", "active")
    await db.execute(
        "INSERT INTO memory_events (event_id, memory_id, type, actor, at) "
        "VALUES ('ev1', 'm1', 'written', 'user', 1000)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db.execute("UPDATE memory_events SET actor = 'model' WHERE event_id = 'ev1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db.execute("DELETE FROM memory_events WHERE event_id = 'ev1'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        await db.execute(
            "INSERT INTO memory_events (event_id, memory_id, type, actor, at) "
            "VALUES ('ev2', 'm_missing', 'written', 'user', 1000)"
        )


async def test_the_blocklist_holds_one_row_per_value(db: aiosqlite.Connection) -> None:
    sql = "INSERT INTO memory_blocklist VALUES ('u_demo', 'home_area', 'global', 'h1', 'dismissed')"
    await db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        await db.execute(sql)


# ------------------------------------------------------------------- transactions


async def test_transaction_immediate_commits_rolls_back_and_does_not_nest(
    db: aiosqlite.Connection,
) -> None:
    async with transaction_immediate(db):
        await db.execute("INSERT INTO users VALUES ('u1', 'Asia/Kolkata', 'One')")
    with pytest.raises(ZeroDivisionError):
        async with transaction_immediate(db):
            await db.execute("INSERT INTO users VALUES ('u2', 'Asia/Kolkata', 'Two')")
            raise ZeroDivisionError
    assert await _rows(db, "SELECT user_id FROM users") == [("u1",)]
    assert not db.in_transaction
    with pytest.raises(RuntimeError, match="does not nest"):
        async with transaction_immediate(db), transaction_immediate(db):
            pass
    assert not db.in_transaction


async def _counter_db(path: Path) -> None:
    conn = await open_agent_db(path)
    await conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    await conn.execute("INSERT INTO counter VALUES (1, 0)")
    await conn.close()


async def _read_then_write(conn: aiosqlite.Connection) -> None:
    (n,) = (await _rows(conn, "SELECT n FROM counter WHERE id = 1"))[0]
    await asyncio.sleep(0)  # let the other connection try to get in between
    await conn.execute("UPDATE counter SET n = ? WHERE id = 1", (n + 1,))


async def test_read_then_write_under_begin_immediate_serializes(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite"
    await _counter_db(path)
    a, b = await connect(path), await connect(path)

    async def worker(conn: aiosqlite.Connection) -> int:
        for _ in range(50):
            async with transaction_immediate(conn):
                await _read_then_write(conn)
        return 50

    assert await asyncio.gather(worker(a), worker(b)) == [50, 50]
    assert await _rows(a, "SELECT n FROM counter") == [(100,)]  # no lost update
    await a.close()
    await b.close()


async def test_control_without_a_transaction_the_same_workers_lose_updates(tmp_path: Path) -> None:
    """Shows the serialization test above is not vacuous: the workers do interleave."""
    path = tmp_path / "agent.sqlite"
    await _counter_db(path)
    a, b = await connect(path), await connect(path)

    async def worker(conn: aiosqlite.Connection) -> None:
        for _ in range(50):
            await _read_then_write(conn)  # autocommit: read and write are separate

    await asyncio.gather(worker(a), worker(b))
    ((n,),) = await _rows(a, "SELECT n FROM counter")
    assert n < 100
    await a.close()
    await b.close()


async def test_control_a_deferred_read_then_write_hits_sqlite_busy(tmp_path: Path) -> None:
    """Why the helper exists: the same read-then-write in a deferred transaction fails."""
    path = tmp_path / "agent.sqlite"
    await _counter_db(path)
    a, b = await connect(path), await connect(path)

    await a.execute("BEGIN")  # deferred: no lock until the first read
    (n,) = (await _rows(a, "SELECT n FROM counter WHERE id = 1"))[0]
    async with transaction_immediate(b):  # another writer commits in between
        await b.execute("UPDATE counter SET n = n + 1 WHERE id = 1")
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await a.execute("UPDATE counter SET n = ? WHERE id = 1", (n + 1,))
    await a.execute("ROLLBACK")
    assert await _rows(a, "SELECT n FROM counter") == [(1,)]
    await a.close()
    await b.close()
