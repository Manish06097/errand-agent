"""ScenarioConfig: the YAML from architecture 5.5, strict blocks, fault names."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from errand.world.scenario import ScenarioConfig, parse_duration

DESIGN_EXAMPLE = """
id: S22_timeout_after_success
seed: 22
clock_start: "2026-11-09T08:00:00+05:30"
catalog: bangalore_v1
user:
  user_id: u_demo
  bookings:
    - { provider: E4, service: electrician, slot: "2026-10-21T10:00+05:30", status: completed }
    - { provider: A2, service: ac_servicing, slot: "2026-11-12T11:00+05:30", status: confirmed }
world:
  enforce_idempotency: true
  book: { forced: [timeout_booked] }
  book_probabilities:
    { success: .70, slot_taken: .15, timeout_booked: .075, timeout_not_booked: .075 }
  quote_drift: { mode: on_hold, providers: { P3: +250 } }
  hold_ttl_s: 600
  reschedule_conflict_slots: ["P2@2026-11-14T10:00+05:30"]
  injection_reviews: default
  events: [{ type: provider_cancelled, booking: "upcoming:0", fire_at: "+30m" }]
  latency_ms: { default: 120, book: 900 }
faults: {}
"""


def test_the_design_example_parses() -> None:
    s = ScenarioConfig.from_yaml(DESIGN_EXAMPLE)
    assert s.world.book.forced == ["timeout_booked"]
    assert s.world.quote_drift.providers == {"P3": 250}
    assert s.world.latency_s("book") == 0.9
    assert s.world.latency_s("search_providers") == 0.12
    assert s.fault_names() == []
    assert s.clock_start.isoformat() == "2026-11-09T08:00:00+05:30"
    assert ScenarioConfig.model_validate_json(s.model_dump_json()) == s


def test_defaults_are_the_contract_world() -> None:
    s = ScenarioConfig(id="d")
    assert s.world.enforce_idempotency is True
    assert s.world.hold_ttl_s == 600
    assert s.world.book_probabilities.as_dict() == {
        "success": 0.70, "slot_taken": 0.15, "timeout_booked": 0.075, "timeout_not_booked": 0.075,
    }  # fmt: skip
    assert s.fault_names() == []
    assert [(b.provider, b.status) for b in s.user.bookings] == [
        ("E4", "completed"),
        ("A2", "confirmed"),
    ]


@pytest.mark.parametrize(
    "yaml_text",
    [
        "id: x\nfaults: { cancle: always_503 }",  # typo'd fault
        "id: x\nworld: { latency: { book: 9 } }",  # typo'd world key
        "id: x\nworld: { book: { forced: [maybe] } }",
        "id: x\nworld: { book_probabilities: { success: .8, slot_taken: .15 } }",  # sums to 1.1
        "id: x\nworld: { quote_drift: { mode: on_hold } }",
        "id: x\nworld: { latency_ms: { bok: 9 } }",
        "id: x\nworld: { reschedule_conflict_slots: ['P2 at 10'] }",
        "id: x\nworld: { events: [{ type: provider_cancelled, booking: B-1, fire_at: +30m }] }",
        "id: x\nfaults: { rehold_slot_shift: { P3: soon } }",
        "id: x\nfaults: { cancel: sometimes }",
        "id: x\nclock_start: '2026-11-09T08:00:00+00:00'",
        "id: x\nextra_top_level: 1",
    ],
)
def test_typos_and_bad_values_fail_loudly(yaml_text: str) -> None:
    with pytest.raises(ValidationError):
        ScenarioConfig.from_yaml(yaml_text)


def test_fault_names_list_only_what_is_switched_on() -> None:
    s = ScenarioConfig.from_yaml(
        "id: f\nfaults:\n  commit_latency_ms: { book: 9000 }\n"
        "  malformed: { tool: get_provider, mode: bad_json }\n"
        "  get_booking_status: { unavailable_n: 5 }\n"
    )
    assert s.fault_names() == ["get_booking_status", "malformed", "commit_latency_ms"]


def test_durations() -> None:
    assert parse_duration("+30m") == timedelta(minutes=30)
    assert parse_duration("-1h") == timedelta(hours=-1)
    assert parse_duration("500ms") == timedelta(milliseconds=500)
    with pytest.raises(ValueError):
        parse_duration("30 minutes")
