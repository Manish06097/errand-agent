"""Cost formula (spike C6), on the spike's real usage_metadata."""

from __future__ import annotations

import json
from typing import Any

import pytest

from errand.llm.pricing import UnknownModelPrice, cost_usd, usage_from_metadata
from errand.usage import Usage
from tests.llm.support import S4_USAGE, SPIKE_OUT


def test_cache_write_call_from_the_spike() -> None:
    usage = usage_from_metadata(S4_USAGE[0])
    # LangChain's input_tokens (1854) already includes the 1843 written to the cache.
    assert usage == Usage(input_uncached=11, cache_write_5m=1843, output=5)
    assert usage.input_total == S4_USAGE[0]["input_tokens"] == 1854
    assert cost_usd("claude-opus-5-5", usage) == 0.009359


def test_cache_read_call_from_the_spike() -> None:
    usage = usage_from_metadata(S4_USAGE[1])
    assert usage == Usage(input_uncached=12, cache_read=1843, output=5)
    assert cost_usd("claude-opus-5-5", usage) == 0.0005166


def test_haiku_reader_call_from_the_spike() -> None:
    reader = json.loads((SPIKE_OUT / "s6_haiku.json").read_text())["enum_only_low"]
    usage = usage_from_metadata(reader["usage"])
    # (997 x 0.10 + 185 x 0.50) / 1e6; the spike rounded it to 0.000192
    assert cost_usd("claude-haiku-5-5", usage) == 0.0001922
    assert round(cost_usd("claude-haiku-5-5", usage), 6) == reader["cost_usd"]


def test_haiku_cache_reads_cost_a_tenth() -> None:
    usage = Usage(input_uncached=0, cache_read=1_000_000)
    assert cost_usd("claude-haiku-5-5", usage) == 0.01
    assert cost_usd("claude-opus-5-5", usage) == 0.2


def test_generic_cache_creation_is_a_fallback_only() -> None:
    no_split: dict[str, Any] = {
        "input_tokens": 110,
        "output_tokens": 0,
        "input_token_details": {"cache_read": 0, "cache_creation": 100},
    }
    assert usage_from_metadata(no_split).cache_write_5m == 100
    # With the TTL split present the generic field must not be added again, even if a
    # future version stopped zeroing it.
    both: dict[str, Any] = {
        "input_tokens": 1854,
        "output_tokens": 5,
        "input_token_details": {
            "cache_read": 0,
            "cache_creation": 1843,
            "ephemeral_5m_input_tokens": 1843,
            "ephemeral_1h_input_tokens": 0,
        },
    }
    assert usage_from_metadata(both) == Usage(input_uncached=11, cache_write_5m=1843, output=5)


def test_one_hour_writes_cost_twice_the_input_price() -> None:
    # SYNTHETIC usage: no spike call wrote a 1 h entry.
    usage = usage_from_metadata(
        {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_token_details": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        }
    )
    assert usage == Usage(cache_write_1h=1_000_000)
    assert cost_usd("claude-opus-5-5", usage) == 8.0


def test_an_unknown_model_is_never_priced_at_zero() -> None:
    with pytest.raises(UnknownModelPrice, match="claude-mystery"):
        cost_usd("claude-mystery", Usage(output=1))


def test_inconsistent_usage_is_rejected() -> None:
    bad: dict[str, Any] = {
        "input_tokens": 10,
        "output_tokens": 1,
        "input_token_details": {"cache_read": 50},
    }
    with pytest.raises(ValueError, match="inconsistent"):
        usage_from_metadata(bad)
