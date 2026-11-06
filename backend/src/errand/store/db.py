"""Connections to agent.sqlite, transactions and migrations.

Every connection is opened with `isolation_level=None`, so Python's sqlite3 module
never starts a transaction of its own. On Python 3.11 its implicit deferred BEGIN
would otherwise either swallow our BEGIN IMMEDIATE or fail with "cannot start a
transaction within a transaction". Every transaction is explicit, through
`transaction_immediate`.

`synchronous` and `foreign_keys` are per-connection settings that do not persist in
the file, so they are set on every connection, not once.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import aiosqlite

BUSY_TIMEOUT_MS = 5000
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA foreign_keys=ON",
)
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


async def connect(path: Path | str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path, isolation_level=None)
    try:
        conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        if row is None or str(row[0]).lower() != "wal":
            raise RuntimeError(f"{path}: journal_mode is {row and row[0]!r}, expected wal")
    except BaseException:
        await conn.close()
        raise
    return conn


@asynccontextmanager
async def transaction_immediate(conn: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the body raises.

    The write lock is taken up front, so a read-then-write either runs or waits for
    the other writer (up to busy_timeout). A deferred transaction would read first and
    then fail with SQLITE_BUSY when it tries to upgrade to a writer.
    """
    if conn.in_transaction:
        raise RuntimeError("transaction_immediate does not nest; a transaction is already open")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            await conn.execute("ROLLBACK")
        raise
    try:
        await conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            await conn.execute("ROLLBACK")
        raise


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    root = resources.files("errand.store").joinpath("migrations")
    found: list[Migration] = []
    for entry in root.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            if entry.name.endswith(".sql"):
                raise ValueError(f"migration {entry.name!r} must be named NNNN_description.sql")
            continue
        found.append(Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migrations must be numbered 1..n without gaps, found {versions}")
    return found


def split_statements(sql: str) -> list[str]:
    """Split a script into statements; sqlite3.complete_statement handles triggers."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()
    ):
        raise ValueError(f"incomplete SQL statement at end of migration: {buf.strip()[:80]!r}")
    return statements


async def migrate(
    conn: aiosqlite.Connection, migrations: Sequence[Migration] | None = None
) -> list[int]:
    """Apply pending migrations in one IMMEDIATE transaction; returns the versions applied.

    The version check happens inside the write lock, so two processes starting at
    once cannot both apply the same migration. Re-running is a no-op.
    """
    migrations = load_migrations() if migrations is None else list(migrations)
    applied: list[int] = []
    async with transaction_immediate(conn):
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        async with conn.execute("SELECT version FROM schema_version") as cur:
            done = {int(row[0]) for row in await cur.fetchall()}
        for migration in migrations:
            if migration.version in done:
                continue
            for statement in split_statements(migration.sql):
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (migration.version,)
            )
            applied.append(migration.version)
    return applied


async def open_agent_db(path: Path | str) -> aiosqlite.Connection:
    """Connect and bring the schema up to date; what the API lifespan calls at startup."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await connect(path)
    try:
        await migrate(conn)
    except BaseException:
        await conn.close()
        raise
    return conn
