"""world.sqlite: the mock provider's own durable store (architecture 5.3, ADR 0004).

All times are integer epoch milliseconds. Every method is synchronous and short: the
engine never awaits inside `tx()`, so no transaction is ever held across a latency
sleep or a timeout, and a slow `book` cannot freeze the rest of the world.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
  provider_id TEXT PRIMARY KEY, name TEXT NOT NULL, service TEXT NOT NULL, area TEXT NOT NULL,
  base_price_inr INTEGER NOT NULL, rating REAL NOT NULL, review_count INTEGER NOT NULL,
  attributes_json TEXT NOT NULL, about TEXT NOT NULL,
  listed INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS slots(
  provider_id TEXT NOT NULL REFERENCES providers, start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('free', 'booked')),
  PRIMARY KEY (provider_id, start_ts));
CREATE TABLE IF NOT EXISTS reviews(
  review_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers,
  rating INTEGER NOT NULL, text TEXT NOT NULL, posted_at INTEGER NOT NULL,
  is_injection INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS holds(
  hold_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, slot_start INTEGER NOT NULL,
  slot_end INTEGER NOT NULL, quoted_price_inr INTEGER NOT NULL, created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'consumed', 'expired', 'released')));
CREATE TABLE IF NOT EXISTS bookings(
  booking_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider_id TEXT NOT NULL,
  service TEXT NOT NULL, slot_start INTEGER NOT NULL, slot_end INTEGER NOT NULL,
  price_inr INTEGER NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('confirmed', 'completed', 'cancelled', 'cancelled_by_provider')),
  hold_id TEXT, created_by_key TEXT, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS bookings_by_key ON bookings(created_by_key);
CREATE INDEX IF NOT EXISTS bookings_by_hold ON bookings(hold_id);
CREATE TABLE IF NOT EXISTS operations(
  idempotency_key TEXT PRIMARY KEY, op TEXT NOT NULL, request_hash TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('in_progress', 'applied', 'not_applied')),
  response_json TEXT, booking_id TEXT,
  calls INTEGER NOT NULL, attempts INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS call_counters(
  tool TEXT NOT NULL, subject TEXT NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (tool, subject));
CREATE TABLE IF NOT EXISTS world_events(
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, target_json TEXT NOT NULL,
  fire_at INTEGER NOT NULL, fired_at INTEGER);
CREATE TABLE IF NOT EXISTS op_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, sim_ts INTEGER NOT NULL, op TEXT NOT NULL,
  idempotency_key TEXT, args_json TEXT NOT NULL, outcome TEXT NOT NULL,
  http_status INTEGER, response_json TEXT, draw REAL, attempt INTEGER,
  done_ts INTEGER, done_seq INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

HoldStatus = Literal["active", "consumed", "expired", "released"]
OpOutcome = Literal["in_progress", "applied", "not_applied"]


@dataclass(frozen=True)
class ProviderRow:
    provider_id: str
    name: str
    service: str
    area: str
    base_price_inr: int
    rating: float
    review_count: int
    attributes: dict[str, Any]
    about: str
    listed: bool


@dataclass(frozen=True)
class ReviewRow:
    review_id: str
    provider_id: str
    rating: int
    text: str
    posted_at: int
    is_injection: bool


@dataclass(frozen=True)
class HoldRow:
    hold_id: str
    provider_id: str
    slot_start: int
    slot_end: int
    quoted_price_inr: int
    created_at: int
    expires_at: int
    status: HoldStatus


@dataclass(frozen=True)
class BookingRow:
    booking_id: str
    user_id: str
    provider_id: str
    service: str
    slot_start: int
    slot_end: int
    price_inr: int
    status: str
    hold_id: str | None
    created_by_key: str | None
    created_at: int


@dataclass(frozen=True)
class OperationRow:
    idempotency_key: str
    op: str
    request_hash: str
    outcome: OpOutcome
    response_json: str | None
    booking_id: str | None
    calls: int
    attempts: int


@dataclass(frozen=True)
class EventRow:
    event_id: str
    type: str
    target: dict[str, Any]
    fire_at: int
    fired_at: int | None


@dataclass(frozen=True)
class OpLogRow:
    seq: int
    sim_ts: int
    op: str
    idempotency_key: str | None
    args_json: str
    outcome: str
    http_status: int | None
    response_json: str | None
    draw: float | None
    attempt: int | None
    done_ts: int | None
    done_seq: int | None


class WorldStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # isolation_level=None: no implicit transactions; tx() is the only way in.
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    @classmethod
    def fresh(cls, path: Path) -> WorldStore:
        """A new, empty store at `path`, replacing whatever was there."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        return cls(path)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _one(self, sql: str, *args: object) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(sql, args).fetchone()
        return row

    def _all(self, sql: str, *args: object) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = self.conn.execute(sql, args).fetchall()
        return rows

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", key)
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def bump_meta(self, key: str) -> int:
        """Increment an integer meta counter and return the new value. Call inside tx()."""
        current = int(self.get_meta(key) or "0") + 1
        self.set_meta(key, str(current))
        return current

    # providers, slots, reviews

    def insert_provider(self, p: ProviderRow) -> None:
        self.conn.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.provider_id,
                p.name,
                p.service,
                p.area,
                p.base_price_inr,
                p.rating,
                p.review_count,
                json.dumps(p.attributes, sort_keys=True),
                p.about,
                int(p.listed),
            ),
        )

    def get_provider(self, provider_id: str) -> ProviderRow | None:
        row = self._one("SELECT * FROM providers WHERE provider_id = ?", provider_id)
        return None if row is None else _provider(row)

    def list_providers(self, service: str, area: str) -> list[ProviderRow]:
        rows = self._all(
            "SELECT * FROM providers WHERE service = ? AND area = ? AND listed = 1",
            service,
            area,
        )
        return sorted((_provider(r) for r in rows), key=lambda p: _id_order(p.provider_id))

    def all_providers(self) -> list[ProviderRow]:
        return [_provider(r) for r in self._all("SELECT * FROM providers")]

    def upsert_slot(self, provider_id: str, start: int, end: int, status: str) -> None:
        self.conn.execute(
            "INSERT INTO slots VALUES (?, ?, ?, ?) ON CONFLICT(provider_id, start_ts)"
            " DO UPDATE SET end_ts = excluded.end_ts, status = excluded.status",
            (provider_id, start, end, status),
        )

    def get_slot(self, provider_id: str, start: int) -> tuple[int, str] | None:
        row = self._one(
            "SELECT end_ts, status FROM slots WHERE provider_id = ? AND start_ts = ?",
            provider_id,
            start,
        )
        return None if row is None else (int(row["end_ts"]), str(row["status"]))

    def set_slot_status(self, provider_id: str, start: int, status: str) -> None:
        self.conn.execute(
            "UPDATE slots SET status = ? WHERE provider_id = ? AND start_ts = ?",
            (status, provider_id, start),
        )

    def free_slots(
        self, provider_id: str, after: int, window: tuple[int, int] | None = None
    ) -> list[tuple[int, int]]:
        """Free slots starting strictly after `after`, optionally within [lo, hi)."""
        lo, hi = window if window is not None else (after, 2**62)
        rows = self._all(
            "SELECT start_ts, end_ts FROM slots WHERE provider_id = ? AND status = 'free'"
            " AND start_ts > ? AND start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            provider_id,
            after,
            lo,
            hi,
        )
        return [(int(r["start_ts"]), int(r["end_ts"])) for r in rows]

    def insert_review(self, r: ReviewRow) -> None:
        self.conn.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
            (r.review_id, r.provider_id, r.rating, r.text, r.posted_at, int(r.is_injection)),
        )

    def reviews_for(self, provider_id: str) -> list[ReviewRow]:
        rows = self._all(
            "SELECT * FROM reviews WHERE provider_id = ? ORDER BY posted_at DESC, review_id",
            provider_id,
        )
        return [_review(r) for r in rows]

    def all_reviews(self) -> list[ReviewRow]:
        return [_review(r) for r in self._all("SELECT * FROM reviews ORDER BY review_id")]

    # holds and bookings

    def insert_hold(self, h: HoldRow) -> None:
        self.conn.execute(
            "INSERT INTO holds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                h.hold_id,
                h.provider_id,
                h.slot_start,
                h.slot_end,
                h.quoted_price_inr,
                h.created_at,
                h.expires_at,
                h.status,
            ),
        )

    def get_hold(self, hold_id: str) -> HoldRow | None:
        row = self._one("SELECT * FROM holds WHERE hold_id = ?", hold_id)
        return None if row is None else _hold(row)

    def count_holds(self, provider_id: str, slot_start: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM holds WHERE provider_id = ? AND slot_start = ?",
            provider_id,
            slot_start,
        )
        return 0 if row is None else int(row["n"])

    def set_hold_status(self, hold_id: str, status: HoldStatus) -> None:
        self.conn.execute("UPDATE holds SET status = ? WHERE hold_id = ?", (status, hold_id))

    def insert_booking(self, b: BookingRow) -> None:
        self.conn.execute(
            "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                b.booking_id,
                b.user_id,
                b.provider_id,
                b.service,
                b.slot_start,
                b.slot_end,
                b.price_inr,
                b.status,
                b.hold_id,
                b.created_by_key,
                b.created_at,
            ),
        )

    def get_booking(self, booking_id: str) -> BookingRow | None:
        row = self._one("SELECT * FROM bookings WHERE booking_id = ?", booking_id)
        return None if row is None else _booking(row)

    def earliest_booking_by(
        self, column: Literal["created_by_key", "hold_id"], value: str
    ) -> BookingRow | None:
        row = self._one(
            f"SELECT * FROM bookings WHERE {column} = ? ORDER BY created_at, rowid LIMIT 1",
            value,
        )
        return None if row is None else _booking(row)

    def bookings_for_hold(self, hold_id: str) -> list[BookingRow]:
        rows = self._all(
            "SELECT * FROM bookings WHERE hold_id = ? ORDER BY created_at, rowid", hold_id
        )
        return [_booking(r) for r in rows]

    def booking_on_slot(self, provider_id: str, slot_start: int) -> BookingRow | None:
        """The live booking holding this slot, if any."""
        row = self._one(
            "SELECT * FROM bookings WHERE provider_id = ? AND slot_start = ?"
            " AND status IN ('confirmed', 'completed') ORDER BY created_at, rowid LIMIT 1",
            provider_id,
            slot_start,
        )
        return None if row is None else _booking(row)

    def bookings_for_user(self, user_id: str) -> list[BookingRow]:
        rows = self._all(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY slot_start, created_at, rowid",
            user_id,
        )
        return [_booking(r) for r in rows]

    def all_bookings(self) -> list[BookingRow]:
        return [_booking(r) for r in self._all("SELECT * FROM bookings ORDER BY created_at, rowid")]

    def set_booking_status(self, booking_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE bookings SET status = ? WHERE booking_id = ?", (status, booking_id)
        )

    def set_booking_slot(self, booking_id: str, start: int, end: int) -> None:
        self.conn.execute(
            "UPDATE bookings SET slot_start = ?, slot_end = ? WHERE booking_id = ?",
            (start, end, booking_id),
        )

    # idempotency

    def get_operation(self, key: str) -> OperationRow | None:
        row = self._one("SELECT * FROM operations WHERE idempotency_key = ?", key)
        return None if row is None else _operation(row)

    def all_operations(self) -> list[OperationRow]:
        return [_operation(r) for r in self._all("SELECT * FROM operations ORDER BY rowid")]

    def count_call(self, key: str, now: int) -> None:
        self.conn.execute(
            "UPDATE operations SET calls = calls + 1, updated_at = ? WHERE idempotency_key = ?",
            (now, key),
        )

    def claim_operation(self, key: str, op: str, request_hash: str, now: int) -> int:
        """Claim `key` for a new evaluation and return its 0-based attempt.

        The key goes in_progress unless it already applied."""
        existing = self.get_operation(key)
        if existing is None:
            self.conn.execute(
                "INSERT INTO operations VALUES (?, ?, ?, 'in_progress', NULL, NULL, 1, 1, ?)",
                (key, op, request_hash, now),
            )
            return 0
        # A dedupe-off re-send on an applied key is evaluated again, but the key stays
        # applied while it runs: a restart mid-latency must not turn it not_applied.
        self.conn.execute(
            "UPDATE operations SET calls = calls + 1, attempts = attempts + 1, updated_at = ?,"
            " outcome = CASE WHEN outcome = 'applied' THEN outcome ELSE 'in_progress' END"
            " WHERE idempotency_key = ?",
            (now, key),
        )
        return existing.attempts

    def finish_operation(
        self,
        key: str,
        outcome: OpOutcome,
        response_json: str | None,
        booking_id: str | None,
        now: int,
    ) -> None:
        # An applied key keeps the record of its first application. Only the
        # dedupe-off mode evaluates an applied key again; what that re-send does is in
        # op_log and bookings, and it must not rewrite the key to not_applied or to a
        # later booking (BUG-M1-2).
        self.conn.execute(
            "UPDATE operations SET updated_at = ?,"
            " response_json = CASE WHEN outcome = 'applied' THEN response_json ELSE ? END,"
            " booking_id = CASE WHEN outcome = 'applied' THEN booking_id"
            " ELSE COALESCE(booking_id, ?) END,"
            " outcome = CASE WHEN outcome = 'applied' THEN outcome ELSE ? END"
            " WHERE idempotency_key = ?",
            (now, response_json, booking_id, outcome, key),
        )

    def abandon_in_progress(self, now: int) -> int:
        """World restart: nothing in flight survived, and the commit is atomic with
        the flip to applied, so every in_progress operation did not apply."""
        cur = self.conn.execute(
            "UPDATE operations SET outcome = 'not_applied', updated_at = ?"
            " WHERE outcome = 'in_progress'",
            (now,),
        )
        self.conn.execute(
            "UPDATE op_log SET outcome = 'interrupted', done_ts = ? WHERE outcome = 'in_progress'",
            (now,),
        )
        return cur.rowcount

    def bump_counter(self, tool: str, subject: str) -> int:
        """Increment a call counter; return how many calls came before this one."""
        row = self._one("SELECT n FROM call_counters WHERE tool = ? AND subject = ?", tool, subject)
        before = 0 if row is None else int(row["n"])
        self.conn.execute(
            "INSERT INTO call_counters VALUES (?, ?, 1) ON CONFLICT(tool, subject)"
            " DO UPDATE SET n = n + 1",
            (tool, subject),
        )
        return before

    # events

    def insert_event(self, event_id: str, type_: str, target: dict[str, Any], fire_at: int) -> None:
        self.conn.execute(
            "INSERT INTO world_events VALUES (?, ?, ?, ?, NULL)",
            (event_id, type_, json.dumps(target, sort_keys=True), fire_at),
        )

    def arm_event(self, event_id: str, target: dict[str, Any], fire_at: int) -> None:
        """Point a waiting `created:N` event at the booking it was waiting for."""
        self.conn.execute(
            "UPDATE world_events SET target_json = ?, fire_at = ? WHERE event_id = ?",
            (json.dumps(target, sort_keys=True), fire_at, event_id),
        )

    def due_events(self, now: int) -> list[EventRow]:
        rows = self._all(
            "SELECT * FROM world_events WHERE fired_at IS NULL AND fire_at <= ?"
            " ORDER BY fire_at, event_id",
            now,
        )
        return [_event(r) for r in rows]

    def mark_fired(self, event_id: str, now: int) -> None:
        self.conn.execute(
            "UPDATE world_events SET fired_at = ? WHERE event_id = ?", (now, event_id)
        )

    def all_events(self) -> list[EventRow]:
        return [_event(r) for r in self._all("SELECT * FROM world_events ORDER BY fire_at")]

    # op_log

    def log_received(
        self, now: int, op: str, key: str | None, args_json: str, outcome: str = "in_progress"
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO op_log(sim_ts, op, idempotency_key, args_json, outcome)"
            " VALUES (?, ?, ?, ?, ?)",
            (now, op, key, args_json, outcome),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def log_done(
        self,
        seq: int,
        now: int,
        outcome: str,
        http_status: int | None,
        response_json: str | None,
        draw: float | None = None,
        attempt: int | None = None,
    ) -> None:
        done_seq = self.bump_meta("done_seq")
        self.conn.execute(
            "UPDATE op_log SET outcome = ?, http_status = ?, response_json = ?, draw = ?,"
            " attempt = ?, done_ts = ?, done_seq = ? WHERE seq = ?",
            (outcome, http_status, response_json, draw, attempt, now, done_seq, seq),
        )

    def log_unfinished(self, seq: int, now: int, outcome: str, http_status: int | None) -> None:
        """Finish `seq` with `outcome` if it is still in_progress; a row that was
        already completed keeps what it recorded."""
        row = self._one("SELECT outcome FROM op_log WHERE seq = ?", seq)
        if row is not None and row["outcome"] == "in_progress":
            self.log_done(seq, now, outcome, http_status, None)

    def op_log(self) -> list[OpLogRow]:
        return [
            OpLogRow(
                seq=int(r["seq"]),
                sim_ts=int(r["sim_ts"]),
                op=str(r["op"]),
                idempotency_key=r["idempotency_key"],
                args_json=str(r["args_json"]),
                outcome=str(r["outcome"]),
                http_status=r["http_status"],
                response_json=r["response_json"],
                draw=r["draw"],
                attempt=r["attempt"],
                done_ts=r["done_ts"],
                done_seq=r["done_seq"],
            )
            for r in self._all("SELECT * FROM op_log ORDER BY seq")
        ]


def _id_order(provider_id: str) -> tuple[str, int]:
    return provider_id[0], int(provider_id[1:])


def _provider(r: sqlite3.Row) -> ProviderRow:
    attributes: dict[str, Any] = json.loads(r["attributes_json"])
    return ProviderRow(
        provider_id=str(r["provider_id"]),
        name=str(r["name"]),
        service=str(r["service"]),
        area=str(r["area"]),
        base_price_inr=int(r["base_price_inr"]),
        rating=float(r["rating"]),
        review_count=int(r["review_count"]),
        attributes=attributes,
        about=str(r["about"]),
        listed=bool(r["listed"]),
    )


def _review(r: sqlite3.Row) -> ReviewRow:
    return ReviewRow(
        review_id=str(r["review_id"]),
        provider_id=str(r["provider_id"]),
        rating=int(r["rating"]),
        text=str(r["text"]),
        posted_at=int(r["posted_at"]),
        is_injection=bool(r["is_injection"]),
    )


def _hold(r: sqlite3.Row) -> HoldRow:
    status: HoldStatus = r["status"]
    return HoldRow(
        hold_id=str(r["hold_id"]),
        provider_id=str(r["provider_id"]),
        slot_start=int(r["slot_start"]),
        slot_end=int(r["slot_end"]),
        quoted_price_inr=int(r["quoted_price_inr"]),
        created_at=int(r["created_at"]),
        expires_at=int(r["expires_at"]),
        status=status,
    )


def _booking(r: sqlite3.Row) -> BookingRow:
    return BookingRow(
        booking_id=str(r["booking_id"]),
        user_id=str(r["user_id"]),
        provider_id=str(r["provider_id"]),
        service=str(r["service"]),
        slot_start=int(r["slot_start"]),
        slot_end=int(r["slot_end"]),
        price_inr=int(r["price_inr"]),
        status=str(r["status"]),
        hold_id=r["hold_id"],
        created_by_key=r["created_by_key"],
        created_at=int(r["created_at"]),
    )


def _operation(r: sqlite3.Row) -> OperationRow:
    outcome: OpOutcome = r["outcome"]
    return OperationRow(
        idempotency_key=str(r["idempotency_key"]),
        op=str(r["op"]),
        request_hash=str(r["request_hash"]),
        outcome=outcome,
        response_json=r["response_json"],
        booking_id=r["booking_id"],
        calls=int(r["calls"]),
        attempts=int(r["attempts"]),
    )


def _event(r: sqlite3.Row) -> EventRow:
    target: dict[str, Any] = json.loads(r["target_json"])
    return EventRow(
        event_id=str(r["event_id"]),
        type=str(r["type"]),
        target=target,
        fire_at=int(r["fire_at"]),
        fired_at=r["fired_at"],
    )
