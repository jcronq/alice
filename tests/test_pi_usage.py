"""Tests for alice_pi.usage.pi_usage_to_info."""

from __future__ import annotations

from alice_pi.usage import pi_usage_to_info


def test_round_trips_pi_shape_to_usage_info() -> None:
    raw = {
        "input": 1050,
        "output": 5,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 1055,
        "cost": {"input": 0.001, "output": 0.0001, "total": 0.0019},
    }
    info = pi_usage_to_info(raw)
    assert info is not None
    assert info.input_tokens == 1050
    assert info.output_tokens == 5
    assert info.cache_read_input_tokens == 0
    assert info.cache_creation_input_tokens == 0
    assert info.total_tokens == 1055


def test_returns_none_for_missing_or_empty() -> None:
    assert pi_usage_to_info(None) is None
    assert pi_usage_to_info({}) is None
    assert pi_usage_to_info("not a dict") is None  # type: ignore[arg-type]


def test_tolerates_missing_fields() -> None:
    info = pi_usage_to_info({"input": 100, "output": 50})
    assert info is not None
    assert info.input_tokens == 100
    assert info.output_tokens == 50
    assert info.cache_read_input_tokens is None
    assert info.cache_creation_input_tokens is None
    assert info.total_tokens is None


def test_tolerates_non_int_extras() -> None:
    info = pi_usage_to_info({"input": 100, "output": 50, "totalTokens": "lots"})
    assert info is not None
    assert info.total_tokens is None
