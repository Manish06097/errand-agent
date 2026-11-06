"""Scheduled world events and quote drift (architecture 5.6).

Provider cancellations are rows in `world_events`. They fire lazily: every world call
and every `tick()` fires the rows whose `fire_at` has passed by the world clock. The
world never calls the agent; the agent finds out through `list_bookings` or
`get_booking_status`, as it would with a real provider.
"""

from __future__ import annotations

import json
from datetime import datetime

from errand.clock import to_ms
from errand.world.rng import draw
from errand.world.scenario import ScenarioConfig
from errand.world.store import BookingRow, EventRow, WorldStore

PROVIDER_CANCELLED = "provider_cancelled"


def schedule(store: WorldStore, scenario: ScenarioConfig, seeded: list[BookingRow]) -> None:
    """Resolve `upcoming:N` and `+30m` against the scenario and store the rows."""
    start = scenario.clock_start
    upcoming = sorted(
        (b for b in seeded if b.status == "confirmed" and b.slot_start > to_ms(start)),
        key=lambda b: (b.slot_start, b.booking_id),
    )
    for i, event in enumerate(scenario.world.events, start=1):
        index = event.upcoming_index()
        if index >= len(upcoming):
            raise ValueError(
                f"event {event.booking} refers past the {len(upcoming)} upcoming seeded bookings"
            )
        store.insert_event(
            f"EV-{i}",
            event.type,
            {"booking_id": upcoming[index].booking_id},
            to_ms(event.resolve_fire_at(start)),
        )


def fire_due(store: WorldStore, now: int) -> list[EventRow]:
    """Fire every event that is due. Call inside `store.tx()`."""
    due = store.due_events(now)
    if not due:
        return []
    for event in due:
        _apply(store, event, now)
    fired = {e.event_id for e in due}
    return [e for e in store.all_events() if e.event_id in fired]


def fire_now(store: WorldStore, booking_id: str, now: int) -> EventRow:
    """Schedule a provider cancellation for `now` and fire it. Call inside `store.tx()`."""
    event_id = f"EV-{store.bump_meta('event_n')}-dev"
    store.insert_event(event_id, PROVIDER_CANCELLED, {"booking_id": booking_id}, now)
    for event in fire_due(store, now):
        if event.event_id == event_id:
            return event
    raise AssertionError("an event scheduled for now did not fire")


def _apply(store: WorldStore, event: EventRow, now: int) -> None:
    if event.type != PROVIDER_CANCELLED:
        raise ValueError(f"unknown world event type {event.type!r}")
    booking_id = str(event.target["booking_id"])
    booking = store.get_booking(booking_id)
    outcome = "skipped"
    if booking is not None and booking.status == "confirmed":
        # The slot stays unavailable: the provider is the one who cannot make it.
        store.set_booking_status(booking_id, "cancelled_by_provider")
        outcome = "fired"
    store.mark_fired(event.event_id, now)
    seq = store.log_received(
        now,
        f"event.{event.type}",
        None,
        json.dumps({"event_id": event.event_id, **event.target}, sort_keys=True),
    )
    store.log_done(seq, now, outcome, None, None)


def quote(
    scenario: ScenarioConfig,
    provider_id: str,
    base_price: int,
    slot_start: datetime,
    prior_holds: int,
) -> tuple[int, float | None]:
    """The held price for one hold, and the draw behind it when there is one."""
    drift = scenario.world.quote_drift
    if drift.mode == "on_hold":
        return base_price + drift.providers.get(provider_id, 0), None
    if drift.mode == "on_rehold":
        delta = drift.providers.get(provider_id, 0) if prior_holds > 0 else 0
        return base_price + delta, None
    if drift.mode == "random_pct":
        assert drift.pct is not None
        if drift.providers and provider_id not in drift.providers:
            return base_price, None
        subject = f"{provider_id}|{slot_start.isoformat()}|{prior_holds}"
        u = draw(scenario.seed, "quote_drift", subject, 0)
        delta = round(base_price * drift.pct / 100 * (2 * u - 1))
        return max(1, base_price + delta), u
    return base_price, None
