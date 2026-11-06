"""Clocks: simulated time moves only when told, demo time is real time plus an offset."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from errand.clock import (
    IST,
    SimClock,
    SystemClock,
    TrackingClock,
    encode_clock_header,
    from_ms,
    parse_clock_header,
    to_ms,
)

START = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


async def test_sim_sleep_advances_instantly_and_yields() -> None:
    seen: list[datetime] = []
    clock = SimClock(START, on_change=seen.append)
    order: list[str] = []

    async def sleeper() -> None:
        await clock.sleep(600)
        order.append("sleeper")

    async def other() -> None:
        order.append("other")

    await asyncio.gather(sleeper(), other())
    assert order == ["other", "sleeper"], "sleep yields to other tasks"
    assert clock.now() == START + timedelta(minutes=10)
    assert seen == [START + timedelta(minutes=10)]
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_ms_round_trip_keeps_ist() -> None:
    assert from_ms(to_ms(START)) == START
    assert from_ms(to_ms(START)).utcoffset() == timedelta(hours=5, minutes=30)
    with pytest.raises(ValueError):
        to_ms(datetime(2026, 11, 9, 8, 0))


def test_system_clock_offset_moves_with_advance() -> None:
    clock = SystemClock(0)
    before = clock.now()
    clock.advance(660)
    assert clock.now() - before >= timedelta(minutes=11)
    assert clock.now().utcoffset() == timedelta(hours=5, minutes=30)


def test_tracking_clock_follows_the_header() -> None:
    clock = TrackingClock()
    with pytest.raises(RuntimeError):
        clock.now()
    clock.update_from_header(encode_clock_header("sim", to_ms(START), 0))
    assert clock.now() == START and clock.mode == "sim"
    reading = parse_clock_header(encode_clock_header("system_offset", 0, 60_000))
    clock.update(reading)
    assert abs((clock.now() - SystemClock(60_000).now()).total_seconds()) < 1
