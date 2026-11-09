"""The mock provider world: the eight tools over world.sqlite, a clock and keyed draws.

`World.handle(tool, payload)` is the one entry point both transports use. It takes the
request as JSON bytes and returns the response as JSON bytes, so the in-process and
HTTP paths share the parser, the encoder and every fault.

Idempotency for the keyed tools (`book`, `cancel`, `reschedule`), architecture 5.3:
- the key is claimed `in_progress` on receipt, in its own short committed transaction;
- an applied key replays its stored response bytes: no new draw, no new booking, and
  an `op_log` row with outcome `replayed`;
- a same-key call that finds the key in progress waits on that key's in-flight latch,
  then replays (applied) or evaluates afresh (not applied);
- a `not_applied` key is evaluated again with a fresh draw (`attempt + 1`);
- a different request under the same key, for any op, is `idempotency_key_reuse`;
- `enforce_idempotency: false` turns off the replay and the wait, so a re-sent `book`
  really is executed again and can double book. That is the point of the mode.

No transaction and no latch is held across a sleep: claim, commit, sleep, then
evaluate and commit, with the hold-expiry check at evaluation time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Protocol, cast

from pydantic import ValidationError

from errand.clock import (
    IST,
    ClockMode,
    SimClock,
    SystemClock,
    encode_clock_header,
    from_ms,
    offset_to,
    to_ms,
)
from errand.world import catalog, events
from errand.world.contracts import (
    KEYED_TOOLS,
    SERVICE_UNAVAILABLE,
    SPECS,
    TOOLS,
    Attributes,
    BookError,
    BookIn,
    BookingItem,
    BookingStatusOut,
    BookOut,
    CancelError,
    CancelIn,
    CancelOut,
    GetBookingStatusIn,
    GetProviderError,
    GetProviderIn,
    GetProviderOut,
    HoldSlotError,
    HoldSlotIn,
    HoldSlotOut,
    ListBookingsIn,
    ListBookingsOut,
    ProviderSummary,
    RescheduleError,
    RescheduleIn,
    RescheduleOut,
    Review,
    SearchProvidersIn,
    SearchProvidersOut,
    Slot,
    ToolName,
    WireModel,
    encode,
    hold_is_live,
)
from errand.world.rng import BookOutcome, book_outcome, draw
from errand.world.scenario import ScenarioConfig
from errand.world.store import (
    BookingRow,
    EventRow,
    HoldRow,
    OperationRow,
    OpLogRow,
    OpOutcome,
    ProviderRow,
    ReviewRow,
    WorldStore,
)

FORCED_NEXT_META = "forced_next_book"


class WorldClosed(RuntimeError):
    """The world was reset or shut down while this call was in flight."""


class UnknownTool(ValueError):
    pass


@dataclass(frozen=True)
class WireResponse:
    status: int
    body: bytes
    # The client must experience a timeout instead of this response. The in-process
    # transport raises after advancing the clock; the HTTP server holds the
    # connection open past the client's timeout.
    wire_timeout: bool = False
    # The op_log row of this call, so the agent's trace can point at the world's record
    # (the HTTP server sends it as WORLD_SEQ_HEADER).
    seq: int | None = None


class Pacing(Protocol):
    """How the world spends time. The transport host decides what a delay means."""

    async def latency(self, seconds: float) -> None: ...

    async def late_commit(self, seconds: float) -> None: ...


class ClockPacing:
    """Every delay is a clock sleep: instant under SimClock, real under SystemClock."""

    def __init__(self, clock: SimClock | SystemClock) -> None:
        self.clock = clock

    async def latency(self, seconds: float) -> None:
        if seconds > 0:
            await self.clock.sleep(seconds)

    async def late_commit(self, seconds: float) -> None:
        await self.clock.sleep(seconds)


@dataclass
class _Result:
    outcome: str
    body: WireModel | None
    http_status: int = 200
    op_outcome: OpOutcome | None = None
    booking_id: str | None = None
    draw: float | None = None
    wire_timeout: bool = False


def _not_applied(outcome: str, body: WireModel, status: int = 200) -> _Result:
    return _Result(outcome, body, http_status=status, op_outcome="not_applied")


def _request_hash(tool: str, model: WireModel) -> str:
    args = model.model_dump(mode="json", by_alias=True, exclude={"idempotency_key"})
    canonical = json.dumps({"op": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _reason(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err["loc"]) or "request"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _slot(start: int, end: int) -> Slot:
    return Slot(start=from_ms(start), end=from_ms(end))


class World:
    def __init__(self, store: WorldStore, scenario: ScenarioConfig, mode: ClockMode) -> None:
        self.store = store
        self.scenario = scenario
        self.mode: ClockMode = mode
        self.clock: SimClock | SystemClock
        if mode == "sim":
            self.clock = SimClock(
                from_ms(int(store.get_meta("sim_now_ms") or "0")), on_change=self._persist_sim
            )
        else:
            self.clock = SystemClock(int(store.get_meta("offset_ms") or "0"))
        self.pacing: Pacing = ClockPacing(self.clock)
        self._latches: dict[str, asyncio.Event] = {}
        self._closed = False

    # lifecycle

    @classmethod
    def create(cls, path: Path, scenario: ScenarioConfig, mode: ClockMode = "sim") -> World:
        """A fresh world.sqlite at `path` holding `scenario`, replacing any previous one."""
        store = WorldStore.fresh(path)
        try:
            with store.tx():
                _seed(store, scenario, mode)
        except BaseException:
            store.close()
            raise
        store.close()
        return cls.open(path)

    @classmethod
    def open(cls, path: Path) -> World:
        """Reopen an existing world, as after a world restart."""
        store = WorldStore(path)
        raw = store.get_meta("scenario_json")
        if raw is None:
            store.close()
            raise FileNotFoundError(f"{path} holds no scenario")
        scenario = ScenarioConfig.model_validate_json(raw)
        mode: ClockMode = "sim" if store.get_meta("clock_mode") == "sim" else "system_offset"
        world = cls(store, scenario, mode)
        with store.tx():
            store.abandon_in_progress(world.now_ms())
        return world

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for latch in self._latches.values():
            latch.set()
        self.store.close()

    def _check_open(self) -> None:
        if self._closed:
            raise WorldClosed("the world was reset while this call was in flight")

    # clock

    def now(self) -> datetime:
        return self.clock.now()

    def now_ms(self) -> int:
        return to_ms(self.clock.now())

    def advance(self, seconds: float) -> datetime:
        self.clock.advance(seconds)
        if isinstance(self.clock, SystemClock):
            with self.store.tx():
                self.store.set_meta("offset_ms", str(self.clock.offset_ms))
        return self.now()

    def clock_header(self) -> str:
        offset = self.clock.offset_ms if isinstance(self.clock, SystemClock) else 0
        return encode_clock_header(self.mode, self.now_ms(), offset)

    def _persist_sim(self, now: datetime) -> None:
        if self._closed:
            return
        if self.store.conn.in_transaction:
            self.store.set_meta("sim_now_ms", str(to_ms(now)))
            return
        with self.store.tx():
            self.store.set_meta("sim_now_ms", str(to_ms(now)))

    # inspection (eval and dev only; never reachable through WorldClient)

    def tick(self) -> list[EventRow]:
        with self.store.tx():
            return events.fire_due(self.store, self.now_ms())

    def fire_provider_cancel(self, booking_id: str) -> EventRow:
        with self.store.tx():
            return events.fire_now(self.store, booking_id, self.now_ms())

    def force_next_book(self, outcome: BookOutcome) -> None:
        """Dev drawer: the next `book` that reaches the outcome draw answers `outcome`.

        A queue in the world's meta table, read ahead of the scenario's `forced` list
        and consumed one per book. Loading a scenario would reset the store, and with
        it the booking in progress, so this is its own narrow route. Test and dev only:
        it is reachable through the inspector, never through WorldClient.
        """
        with self.store.tx():
            queue = json.loads(self.store.get_meta(FORCED_NEXT_META) or "[]")
            queue.append(outcome)
            self.store.set_meta(FORCED_NEXT_META, json.dumps(queue))

    def _pop_forced_next(self) -> BookOutcome | None:
        queue = json.loads(self.store.get_meta(FORCED_NEXT_META) or "[]")
        if not queue:
            return None
        self.store.set_meta(FORCED_NEXT_META, json.dumps(queue[1:]))
        outcome: BookOutcome = queue[0]
        return outcome

    def events(self) -> list[EventRow]:
        return self.store.all_events()

    def op_log(self) -> list[OpLogRow]:
        return self.store.op_log()

    def bookings(self) -> list[BookingRow]:
        return self.store.all_bookings()

    def operations(self) -> list[OperationRow]:
        return self.store.all_operations()

    def reviews(self) -> list[ReviewRow]:
        return self.store.all_reviews()

    # the tools

    async def handle(self, tool: str, payload: bytes) -> WireResponse:
        if tool not in TOOLS:
            raise UnknownTool(tool)
        name: ToolName = tool
        if self.scenario.faults.duplicate_delivery == name:
            # A retrying proxy: the same request reaches the provider twice and the
            # client sees the answer to the second delivery.
            await self._handle_once(name, payload)
        return await self._handle_once(name, payload)

    async def _handle_once(self, tool: ToolName, payload: bytes) -> WireResponse:
        self._check_open()
        args_json = payload.decode("utf-8", errors="replace")
        spec = SPECS[tool]
        try:
            model = spec.input.model_validate_json(payload)
        except ValidationError as exc:
            return self._reject(tool, args_json, _reason(exc))
        if tool in KEYED_TOOLS:
            return await self._keyed(tool, model, args_json)
        return await self._plain(tool, model, args_json)

    def _reject(self, tool: ToolName, args_json: str, reason: str) -> WireResponse:
        key = None
        try:
            raw = json.loads(args_json)
            if isinstance(raw, dict) and isinstance(raw.get("idempotency_key"), str):
                key = raw["idempotency_key"]
        except ValueError:
            pass
        body = SPECS[tool].error.model_validate({"error": "invalid_args", "reason": reason})
        with self.store.tx():
            now = self.now_ms()
            seq = self.store.log_received(now, tool, key, args_json)
            wire = self._wire(tool, _Result("invalid_args", body))
            self.store.log_done(seq, now, "invalid_args", 200, wire.body.decode(errors="replace"))
        return replace(wire, seq=seq)

    async def _plain(self, tool: ToolName, model: WireModel, args_json: str) -> WireResponse:
        with self.store.tx():
            seq = self.store.log_received(self.now_ms(), tool, None, args_json)
        with self._finishes_row(seq):
            await self.pacing.latency(self.scenario.world.latency_s(tool))
            self._check_open()
            with self.store.tx():
                now = self.now_ms()
                events.fire_due(self.store, now)
                result = self._evaluate_plain(tool, model, now)
                wire = self._wire(tool, result)
                self.store.log_done(
                    seq,
                    now,
                    result.outcome,
                    wire.status,
                    wire.body.decode(errors="replace"),
                    result.draw,
                )
            return replace(wire, seq=seq)

    async def _keyed(self, tool: ToolName, model: WireModel, args_json: str) -> WireResponse:
        assert isinstance(model, BookIn | CancelIn | RescheduleIn)
        key = model.idempotency_key
        request_hash = _request_hash(tool, model)
        with self.store.tx():
            seq = self.store.log_received(self.now_ms(), tool, key, args_json)
        with self._finishes_row(seq):
            wire = await self._keyed_call(tool, model, key, request_hash, seq)
            return replace(wire, seq=seq)

    async def _keyed_call(
        self,
        tool: ToolName,
        model: BookIn | CancelIn | RescheduleIn,
        key: str,
        request_hash: str,
        seq: int,
    ) -> WireResponse:
        enforce = self.scenario.world.enforce_idempotency
        # Check and claim. There is no await between reading the operation row and
        # committing the claim, so on one event loop nothing can interleave there;
        # waiting for an in-flight call happens outside, on that key's latch.
        while True:
            self._check_open()
            now = self.now_ms()
            op = self.store.get_operation(key)
            if op is not None and op.request_hash != request_hash:
                return self._finish_early(
                    tool, seq, now, "idempotency_key_reuse", {"error": "idempotency_key_reuse"}
                )
            if enforce and op is not None and op.outcome == "applied":
                assert op.response_json is not None
                with self.store.tx():
                    self.store.count_call(key, now)
                    wire = self._wire_bytes(tool, 200, op.response_json.encode())
                    self.store.log_done(
                        seq, now, "replayed", 200, wire.body.decode(errors="replace")
                    )
                return wire
            latch = self._latches.get(key) if enforce else None
            if op is not None and op.outcome == "in_progress" and latch is not None:
                await latch.wait()
                continue
            with self.store.tx():
                attempt = self.store.claim_operation(key, tool, request_hash, now)
            mine = asyncio.Event()
            if enforce:
                self._latches[key] = mine
            break

        try:
            await self.pacing.latency(self.scenario.world.latency_s(tool))
            late = self.scenario.faults.commit_latency_ms
            if tool == "book" and late is not None:
                await self.pacing.late_commit(late.book / 1000)
            self._check_open()
            with self.store.tx():
                now = self.now_ms()
                events.fire_due(self.store, now)
                result = self._evaluate_keyed(tool, model, key, attempt, now)
                assert result.op_outcome is not None
                stored = None if result.body is None else encode(result.body).decode()
                self.store.finish_operation(key, result.op_outcome, stored, result.booking_id, now)
                if result.wire_timeout:
                    wire = WireResponse(504, b"", wire_timeout=True)
                    logged = stored
                    status: int | None = None
                else:
                    wire = self._wire(tool, result)
                    logged = wire.body.decode(errors="replace")
                    status = wire.status
                self.store.log_done(seq, now, result.outcome, status, logged, result.draw, attempt)
            return wire
        except BaseException:
            if not self._closed:
                with self.store.tx():
                    self.store.finish_operation(key, "not_applied", None, None, self.now_ms())
            raise
        finally:
            mine.set()
            if self._latches.get(key) is mine:
                del self._latches[key]

    @contextmanager
    def _finishes_row(self, seq: int) -> Iterator[None]:
        """Every call ends with a finished op_log row, even when evaluation raises.

        An exception is a bug in the world: the row says internal_error (the HTTP
        client gets a 500) and the exception propagates. A cancelled call is
        interrupted, the same outcome a restart gives. A world that was reset
        meanwhile has no store to write to, and its log is gone anyway.
        """
        try:
            yield
        except BaseException as exc:
            if not self._closed:
                failed = isinstance(exc, Exception)
                with self.store.tx():
                    self.store.log_unfinished(
                        seq,
                        self.now_ms(),
                        "internal_error" if failed else "interrupted",
                        500 if failed else None,
                    )
            raise

    def _finish_early(
        self, tool: ToolName, seq: int, now: int, outcome: str, body: dict[str, str]
    ) -> WireResponse:
        model = SPECS[tool].error.model_validate(body)
        with self.store.tx():
            wire = self._wire(tool, _Result(outcome, model))
            self.store.log_done(seq, now, outcome, wire.status, wire.body.decode(errors="replace"))
        return wire

    # wire faults

    def _wire(self, tool: ToolName, result: _Result) -> WireResponse:
        assert result.body is not None
        return self._wire_bytes(tool, result.http_status, encode(result.body))

    def _wire_bytes(self, tool: ToolName, status: int, body: bytes) -> WireResponse:
        malformed = self.scenario.faults.malformed
        if malformed is not None and malformed.tool == tool:
            body = _corrupt(body, malformed.mode)
        return WireResponse(status, body)

    # evaluation, inside one transaction

    def _evaluate_plain(self, tool: ToolName, model: WireModel, now: int) -> _Result:
        faults = self.scenario.faults
        if tool == "search_providers":
            if faults.search_providers is not None:
                return _Result("service_unavailable", SERVICE_UNAVAILABLE, 503)
            return self._search(cast(SearchProvidersIn, model), now)
        if tool == "get_provider":
            if faults.get_provider is not None:
                return _Result("service_unavailable", SERVICE_UNAVAILABLE, 503)
            return self._get_provider(cast(GetProviderIn, model), now)
        if tool == "hold_slot":
            return self._hold(cast(HoldSlotIn, model), now)
        if tool == "get_booking_status":
            outage = faults.get_booking_status
            if outage is not None:
                calls_before = self.store.bump_counter("get_booking_status", "*")
                if calls_before < outage.unavailable_n:
                    return _Result("service_unavailable", SERVICE_UNAVAILABLE, 503)
            return self._status(cast(GetBookingStatusIn, model))
        if tool == "list_bookings":
            return self._list(cast(ListBookingsIn, model))
        raise AssertionError(f"{tool} is not a plain tool")

    def _evaluate_keyed(
        self, tool: ToolName, model: WireModel, key: str, attempt: int, now: int
    ) -> _Result:
        if tool == "book":
            return self._book(cast(BookIn, model), key, attempt, now)
        if tool == "cancel":
            return self._cancel(cast(CancelIn, model), now)
        if tool == "reschedule":
            return self._reschedule(cast(RescheduleIn, model), now)
        raise AssertionError(f"{tool} is not a keyed tool")

    def _summary(self, p: ProviderRow, slots: list[tuple[int, int]]) -> ProviderSummary:
        return ProviderSummary.model_validate(
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "service": p.service,
                "area": p.area,
                "price_inr": p.base_price_inr,
                "rating": p.rating,
                "review_count": p.review_count,
                "available_slots": [_slot(s, e) for s, e in slots],
                "attributes": Attributes.model_validate(p.attributes),
            },
            strict=False,
        )

    def _search(self, req: SearchProvidersIn, now: int) -> _Result:
        lo = to_ms(datetime.combine(req.date_range.from_, time(0), IST))
        hi = to_ms(datetime.combine(req.date_range.to + timedelta(days=1), time(0), IST))
        providers = [
            self._summary(p, self.store.free_slots(p.provider_id, now, (lo, hi)))
            for p in self.store.list_providers(req.service.value, req.area.value)
        ]
        return _Result("ok", SearchProvidersOut(providers=providers))

    def _get_provider(self, req: GetProviderIn, now: int) -> _Result:
        p = self.store.get_provider(req.provider_id)
        if p is None:
            return _Result("not_found", GetProviderError(error="not_found"))
        reviews = [
            Review(
                review_id=r.review_id, rating=r.rating, text=r.text, posted_at=from_ms(r.posted_at)
            )
            for r in self.store.reviews_for(p.provider_id)
        ]
        body = GetProviderOut.model_validate(
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "service": p.service,
                "area": p.area,
                "price_inr": p.base_price_inr,
                "rating": p.rating,
                "attributes": Attributes.model_validate(p.attributes),
                "about": p.about,
                "available_slots": [
                    _slot(s, e) for s, e in self.store.free_slots(p.provider_id, now)
                ],
                "reviews": reviews,
            },
            strict=False,
        )
        return _Result("ok", body)

    def _hold(self, req: HoldSlotIn, now: int) -> _Result:
        p = self.store.get_provider(req.provider_id)
        if p is None:
            return _Result(
                "invalid_args", HoldSlotError(error="invalid_args", reason="unknown provider")
            )
        start, end = to_ms(req.slot.start), to_ms(req.slot.end)
        slot = self.store.get_slot(p.provider_id, start)
        if slot is None or slot[0] != end:
            reason = "not one of the provider's slots"
            return _Result("invalid_args", HoldSlotError(error="invalid_args", reason=reason))
        if start <= now:
            reason = "slot has already started"
            return _Result("invalid_args", HoldSlotError(error="invalid_args", reason=reason))
        if slot[1] == "booked":
            reason = "slot is not free"
            return _Result("invalid_args", HoldSlotError(error="invalid_args", reason=reason))
        # Holds never supersede each other: an existing live hold on this slot stays
        # valid and this one is created next to it.
        prior = self.store.count_holds(p.provider_id, start)
        price, u = events.quote(
            self.scenario, p.provider_id, p.base_price_inr, req.slot.start, prior
        )
        shift = self.scenario.faults.slot_shift(p.provider_id) if prior > 0 else None
        if shift is not None:
            delta = int(shift.total_seconds() * 1000)
            start, end = start + delta, end + delta
        hold = HoldRow(
            hold_id=f"H-{self.store.bump_meta('hold_n')}",
            provider_id=p.provider_id,
            slot_start=start,
            slot_end=end,
            quoted_price_inr=price,
            created_at=now,
            expires_at=now + self.scenario.world.hold_ttl_s * 1000,
            status="active",
        )
        self.store.insert_hold(hold)
        body = HoldSlotOut(
            hold_id=hold.hold_id,
            provider_id=hold.provider_id,
            slot=_slot(hold.slot_start, hold.slot_end),
            quoted_price_inr=hold.quoted_price_inr,
            expires_at=from_ms(hold.expires_at),
        )
        return _Result("ok", body, draw=u)

    def _slot_taken_by_other(self, hold: HoldRow, key: str) -> bool:
        holder = self.store.booking_on_slot(hold.provider_id, hold.slot_start)
        if holder is not None:
            # With dedupe off, a re-sent request under the same key is executed
            # again; the provider does not notice it is its own booking.
            return holder.created_by_key != key
        slot = self.store.get_slot(hold.provider_id, hold.slot_start)
        return slot is not None and slot[1] == "booked"

    def _used_by_another_key(self, hold: HoldRow, key: str) -> _Result | None:
        """A consumed hold never books under a key other than the one that consumed it.

        Checking only the slot is not enough: once the consuming booking is cancelled
        or moved, the slot is free again. The consuming key itself falls through, so a
        dedupe-off re-send still executes again as decided in 5.3."""
        if hold.status != "consumed":
            return None
        made = self.store.bookings_for_hold(hold.hold_id)
        if any(b.created_by_key == key for b in made):
            return None
        if any(b.status in ("confirmed", "completed") for b in made):
            return _not_applied("slot_taken", BookError(error="slot_taken"))
        reason = "hold already used"
        return _not_applied("invalid_args", BookError(error="invalid_args", reason=reason))

    def _book(self, req: BookIn, key: str, attempt: int, now: int) -> _Result:
        hold = self.store.get_hold(req.hold_id)
        if hold is None:
            return _not_applied(
                "invalid_args", BookError(error="invalid_args", reason="unknown hold")
            )
        # 1. Past expiry by the world clock. Nothing else returns hold_expired.
        if not hold_is_live(from_ms(now), from_ms(hold.expires_at)):
            if hold.status == "active":
                self.store.set_hold_status(hold.hold_id, "expired")
            return _not_applied("hold_expired", BookError(error="hold_expired"))
        refused = self._used_by_another_key(hold, key)
        if refused is not None:
            return refused
        if hold.status == "released" or self._slot_taken_by_other(hold, key):
            if hold.status == "active":
                self.store.set_hold_status(hold.hold_id, "released")
            return _not_applied("slot_taken", BookError(error="slot_taken"))

        # 2. The scenario's forced list, consumed in order; 3. otherwise the keyed draw.
        forced = self.scenario.world.book.forced
        used = int(self.store.get_meta("forced_book_used") or "0")
        u: float | None = None
        pushed = self._pop_forced_next()
        if pushed is not None:
            outcome = pushed
        elif used < len(forced):
            outcome = forced[used]
            self.store.set_meta("forced_book_used", str(used + 1))
        else:
            u = draw(self.scenario.seed, "book", key, attempt)
            outcome = book_outcome(u, self.scenario.world.book_probabilities.as_dict())

        if outcome == "slot_taken":
            # "Another customer" got there first.
            self._occupy(hold.provider_id, hold.slot_start, hold.slot_end)
            if hold.status == "active":
                self.store.set_hold_status(hold.hold_id, "released")
            return _Result(
                "slot_taken", BookError(error="slot_taken"), op_outcome="not_applied", draw=u
            )
        if outcome == "timeout_not_booked":
            return _Result(outcome, None, op_outcome="not_applied", draw=u, wire_timeout=True)

        provider = self.store.get_provider(hold.provider_id)
        assert provider is not None
        mismatch = self.scenario.faults.book_price_mismatch or 0
        booking = BookingRow(
            booking_id=f"B-{self.store.bump_meta('booking_n')}",
            user_id=self.scenario.user.user_id,
            provider_id=hold.provider_id,
            service=provider.service,
            slot_start=hold.slot_start,
            slot_end=hold.slot_end,
            price_inr=hold.quoted_price_inr + mismatch,
            status="confirmed",
            hold_id=hold.hold_id,
            created_by_key=key,
            created_at=now,
        )
        self.store.insert_booking(booking)
        events.arm_created(self.store, booking.booking_id, now)
        self.store.set_hold_status(hold.hold_id, "consumed")
        self._occupy(hold.provider_id, hold.slot_start, hold.slot_end)
        body = BookOut(
            status="confirmed",
            booking_id=booking.booking_id,
            provider_id=booking.provider_id,
            slot=_slot(booking.slot_start, booking.slot_end),
            price_inr=booking.price_inr,
        )
        # For timeout_booked the stored response is this success body: the timeout
        # happened on the wire, not at the provider.
        return _Result(
            outcome,
            body,
            op_outcome="applied",
            booking_id=booking.booking_id,
            draw=u,
            wire_timeout=outcome == "timeout_booked",
        )

    def _occupy(self, provider_id: str, start: int, end: int) -> None:
        self.store.upsert_slot(provider_id, start, end, "booked")

    def _release(self, provider_id: str, start: int) -> None:
        if self.store.booking_on_slot(provider_id, start) is None:
            self.store.set_slot_status(provider_id, start, "free")

    def _status(self, req: GetBookingStatusIn) -> _Result:
        if req.hold_id is not None:
            booking = self.store.earliest_booking_by("hold_id", req.hold_id)
        else:
            assert req.idempotency_key is not None
            booking = self.store.earliest_booking_by("created_by_key", req.idempotency_key)
        if booking is None:
            return _Result("not_found", BookingStatusOut(status="not_found"))
        status = "confirmed" if booking.status == "completed" else booking.status
        body = BookingStatusOut.model_validate(
            {
                "status": status,
                "booking_id": booking.booking_id,
                "slot": _slot(booking.slot_start, booking.slot_end),
            },
            strict=False,
        )
        return _Result(body.status, body)

    def _cancel(self, req: CancelIn, now: int) -> _Result:
        booking = self.store.get_booking(req.booking_id)
        if booking is None:
            return _not_applied(
                "invalid_args", CancelError(error="invalid_args", reason="unknown booking")
            )
        if booking.status == "cancelled":
            # Effect-idempotent: cancelling a cancelled booking changes nothing.
            return _Result(
                "ok",
                CancelOut(status="cancelled", booking_id=booking.booking_id),
                op_outcome="applied",
                booking_id=booking.booking_id,
            )
        if booking.status != "confirmed":
            reason = f"booking is {booking.status}"
            return _not_applied("invalid_args", CancelError(error="invalid_args", reason=reason))
        calls_before = self.store.bump_counter("cancel", booking.booking_id)
        if calls_before == 0 or self.scenario.faults.cancel == "always_503":
            # The contract: the first call per booking fails with a transient 503.
            return _not_applied("service_unavailable", SERVICE_UNAVAILABLE, 503)
        self.store.set_booking_status(booking.booking_id, "cancelled")
        self._release(booking.provider_id, booking.slot_start)
        return _Result(
            "ok",
            CancelOut(status="cancelled", booking_id=booking.booking_id),
            op_outcome="applied",
            booking_id=booking.booking_id,
        )

    def _reschedule(self, req: RescheduleIn, now: int) -> _Result:
        booking = self.store.get_booking(req.booking_id)
        if booking is None:
            reason = "unknown booking"
            return _not_applied(
                "invalid_args", RescheduleError(error="invalid_args", reason=reason)
            )
        if booking.status != "confirmed":
            reason = f"booking is {booking.status}"
            return _not_applied(
                "invalid_args", RescheduleError(error="invalid_args", reason=reason)
            )
        start, end = to_ms(req.slot.start), to_ms(req.slot.end)
        done = _Result(
            "ok",
            RescheduleOut(status="rescheduled", booking_id=booking.booking_id, slot=req.slot),
            op_outcome="applied",
            booking_id=booking.booking_id,
        )
        if (start, end) == (booking.slot_start, booking.slot_end):
            return done  # effect-idempotent: already there
        conflicts = {(pid, to_ms(when)) for pid, when in self.scenario.world.conflict_slots()}
        if (booking.provider_id, start) in conflicts:
            return _not_applied("slot_conflict", RescheduleError(error="slot_conflict"))
        slot = self.store.get_slot(booking.provider_id, start)
        if slot is None or slot[0] != end:
            reason = "not one of the provider's slots"
            return _not_applied(
                "invalid_args", RescheduleError(error="invalid_args", reason=reason)
            )
        if start <= now:
            reason = "slot has already started"
            return _not_applied(
                "invalid_args", RescheduleError(error="invalid_args", reason=reason)
            )
        if slot[1] == "booked":
            return _not_applied("slot_conflict", RescheduleError(error="slot_conflict"))
        old_start = booking.slot_start
        self.store.set_booking_slot(booking.booking_id, start, end)
        self._occupy(booking.provider_id, start, end)
        self._release(booking.provider_id, old_start)
        return done

    def _list(self, req: ListBookingsIn) -> _Result:
        names: dict[str, str] = {}
        items = []
        for b in self.store.bookings_for_user(req.user_id):
            if b.provider_id not in names:
                provider = self.store.get_provider(b.provider_id)
                names[b.provider_id] = provider.name if provider else b.provider_id
            items.append(
                BookingItem.model_validate(
                    {
                        "booking_id": b.booking_id,
                        "provider_id": b.provider_id,
                        "provider_name": names[b.provider_id],
                        "service": b.service,
                        "slot": _slot(b.slot_start, b.slot_end),
                        "status": b.status,
                    },
                    strict=False,
                )
            )
        return _Result("ok", ListBookingsOut(bookings=items))


def _seed(store: WorldStore, scenario: ScenarioConfig, mode: ClockMode) -> None:
    start = scenario.clock_start
    store.set_meta("scenario_json", scenario.model_dump_json())
    store.set_meta("scenario_id", scenario.id)
    store.set_meta("seed", str(scenario.seed))
    store.set_meta("clock_mode", mode)
    store.set_meta("sim_now_ms", str(to_ms(start)))
    store.set_meta("offset_ms", str(offset_to(start) if mode == "system_offset" else 0))
    # World ids come from the world's own counters, started at a seed-dependent value
    # so nothing downstream can get away with hard-coding them.
    for kind in ("hold_n", "booking_n"):
        store.set_meta(kind, str(1000 + int(draw(scenario.seed, "ids", kind, 0) * 8000)))
    catalog.build(store, scenario)

    seeded: list[BookingRow] = []
    for sb in scenario.user.bookings:
        provider = store.get_provider(sb.provider)
        if provider is None:
            raise ValueError(f"seeded booking with {sb.provider}, which is not in the catalog")
        if provider.service != sb.service.value:
            raise ValueError(f"seeded booking: {sb.provider} is a {provider.service}")
        slot_start = to_ms(sb.slot)
        slot_end = to_ms(sb.slot + timedelta(hours=1))
        booking = BookingRow(
            booking_id=f"B-{store.bump_meta('booking_n')}",
            user_id=scenario.user.user_id,
            provider_id=sb.provider,
            service=provider.service,
            slot_start=slot_start,
            slot_end=slot_end,
            price_inr=provider.base_price_inr,
            status=sb.status,
            hold_id=None,
            created_by_key=None,
            created_at=to_ms(start),
        )
        store.insert_booking(booking)
        if sb.status in ("confirmed", "completed"):
            store.upsert_slot(sb.provider, slot_start, slot_end, "booked")
        seeded.append(booking)
    events.schedule(store, scenario, seeded)


def _corrupt(body: bytes, mode: str) -> bytes:
    if mode == "bad_json":
        return body[: max(1, len(body) // 2)]
    data = json.loads(body)
    if not isinstance(data, dict):
        return body
    if mode == "extra_fields":
        data["internal_rank_score"] = 0.97
    elif mode == "wrong_types":
        _retype(data)
    return json.dumps(data).encode()


def _retype(data: dict[str, object]) -> None:
    """Give one value the wrong type: the first number, else the first string, else
    the first top-level value. The searches go to any depth because some bodies are
    a single list (search_providers, list_bookings); a top-level-only rewrite left
    those valid, and an empty list has no scalar at all (BUG-M1-1)."""
    if _replace_first(data, _is_number, str) or _replace_first(data, _is_string, _int):
        return
    for key, value in data.items():
        data[key] = json.dumps(value)
        return


def _int(_: object) -> object:
    return 12345


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _replace_first(
    node: object, match: Callable[[object], bool], replace: Callable[[object], object]
) -> bool:
    """Depth-first, in document order: replace the first scalar `match` accepts."""
    if isinstance(node, dict):
        for key, value in node.items():
            if match(value):
                node[key] = replace(value)
                return True
            if _replace_first(value, match, replace):
                return True
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if match(value):
                node[index] = replace(value)
                return True
            if _replace_first(value, match, replace):
                return True
    return False


class WorldHost:
    """Owns the current World at one path and swaps it on a scenario load or reset.

    The HTTP server and the in-process transport both hold a host, so a reset gives
    every caller the fresh world at once.
    """

    def __init__(self, path: Path, pacing: Callable[[World], Pacing] | None = None) -> None:
        self.path = path
        self._pacing = pacing
        self._world: World | None = None

    @property
    def loaded(self) -> bool:
        return self._world is not None

    @property
    def world(self) -> World:
        if self._world is None:
            raise RuntimeError("no scenario is loaded")
        return self._world

    def load(self, scenario: ScenarioConfig, mode: ClockMode = "sim") -> World:
        self.close()
        return self._adopt(World.create(self.path, scenario, mode))

    def reset(self, mode: ClockMode | None = None) -> World:
        current = self.world
        return self.load(current.scenario, mode or current.mode)

    def reopen(self) -> World | None:
        """Pick up an existing world.sqlite after a restart; None if there is none."""
        self.close()
        if not self.path.exists():
            return None
        try:
            return self._adopt(World.open(self.path))
        except FileNotFoundError:
            return None

    def close(self) -> None:
        if self._world is not None:
            self._world.close()
            self._world = None

    def _adopt(self, world: World) -> World:
        if self._pacing is not None:
            world.pacing = self._pacing(world)
        self._world = world
        return world
