"""The gate 2 context caps (architecture 13.1) sum to the 12,000-token budget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from errand.config import ContextCaps, LimitSettings
from errand.context.builder import CAPS


def test_the_caps_sum_to_the_budget() -> None:
    caps = ContextCaps()
    assert caps.total == LimitSettings().context_budget_tokens == 12_000


def test_the_gate_2_table() -> None:
    caps = ContextCaps()
    assert (caps.rules, caps.tools, caps.state, caps.memory, caps.ledger) == (
        1_600,
        3_800,
        1_100,
        600,
        400,
    )
    assert (caps.recent, caps.current, caps.loop, caps.slack) == (1_000, 1_000, 2_300, 200)


def test_caps_that_do_not_sum_to_the_budget_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRAND_CAP_TOOLS", "3000")
    with pytest.raises(ValidationError, match="sum to 11200, not 12000"):
        ContextCaps()
    monkeypatch.setenv("ERRAND_CAP_TOOLS", "3800")
    monkeypatch.setenv("ERRAND_CAP_SLACK", "300")
    with pytest.raises(ValidationError, match="sum to 12100"):
        ContextCaps()


def test_the_builder_reads_the_same_numbers() -> None:
    caps = ContextCaps()
    assert {
        "rules": caps.rules,
        "tools": caps.tools,
        "state": caps.state,
        "recent": caps.recent,
        "current": caps.current,
    } == CAPS
