"""The live run deadline must leave the quarantined reader its timeout (architecture 15.1).

The reader is skipped once less run time is left than its own timeout. A deadline of
only 2 x p95 skipped it after about 3 s on every cold-cache live turn (M7 review round 1:
every reader.call came back `error: deadline`).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from errand.config import LIVE_PLANNING_TURN_P95_S, LimitSettings, ReaderSettings
from errand.domain.counters import RunCounters
from errand.quarantine.reader import ReviewReader


def test_deadline_is_twice_p95_plus_the_reader_timeout() -> None:
    limits, reader = LimitSettings(), ReaderSettings()
    assert limits.live_run_deadline_s - reader.timeout_s >= 2 * LIVE_PLANNING_TURN_P95_S
    assert limits.live_run_deadline_s == math.ceil(2 * LIVE_PLANNING_TURN_P95_S) + reader.timeout_s


def test_a_reader_call_at_t_8s_of_a_live_run_is_not_skipped() -> None:
    limits, settings = LimitSettings(), ReaderSettings()
    start = datetime(2026, 11, 1, 10, tzinfo=UTC)
    counters = RunCounters(
        started_at=start, deadline_at=start + timedelta(seconds=limits.live_run_deadline_s)
    )
    stand_in = SimpleNamespace(
        counters=counters,
        settings=settings,
        limits=limits,
        clock=SimpleNamespace(now=lambda: start + timedelta(seconds=8)),
    )
    assert counters.cost < Decimal(str(limits.run_cost_cap_usd))
    assert ReviewReader._refusal_reason(stand_in) is None  # type: ignore[arg-type]
    # And the rule still bites once the time really is short.
    late = SimpleNamespace(
        **{
            **vars(stand_in),
            "clock": SimpleNamespace(
                now=lambda: start + timedelta(seconds=limits.live_run_deadline_s - 1)
            ),
        }
    )
    assert ReviewReader._refusal_reason(late) == "deadline"  # type: ignore[arg-type]
