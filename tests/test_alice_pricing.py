"""Tests for ``alice_pricing``: rate lookup, estimate math, actuals parsing.

Rate values are pinned to the ``PRICING_SOURCE_DATE`` table so a
drift in rates.py is caught by a red test, not silent cost
mis-reporting.
"""

from __future__ import annotations

import json

import pytest

from alice_pricing import (
    PRICING_SOURCE_DATE,
    actual_cost_from_tokens,
    estimate_cost,
    estimate_tokens,
    format_usd,
    read_worker_tokens,
    rates_for,
)
from alice_pricing.rates import RATES, is_free


# ---------------------------------------------------------------- rates


def test_pricing_source_date_is_set():
    """A date string is present so audits can trace back to the pull."""
    assert PRICING_SOURCE_DATE
    # ISO ``YYYY-MM-DD`` form.
    assert PRICING_SOURCE_DATE.count("-") == 2
    assert len(PRICING_SOURCE_DATE) == 10


def test_rates_for_canonical_opus_4_7():
    r = rates_for("claude-opus-4-7")
    assert r["input_per_mtok"] == 5.00
    assert r["output_per_mtok"] == 25.00
    assert r["cache_write_per_mtok"] == 6.25
    assert r["cache_read_per_mtok"] == 0.50


def test_rates_for_sonnet_family():
    r = rates_for("claude-sonnet-4-6")
    assert r["input_per_mtok"] == 3.00
    assert r["output_per_mtok"] == 15.00


def test_rates_for_haiku_family():
    r = rates_for("claude-haiku-4-5")
    assert r["input_per_mtok"] == 1.00
    assert r["output_per_mtok"] == 5.00


def test_rates_for_alias_short_name():
    """Short names like ``opus`` and ``opus-4-7`` resolve to canonical ids."""
    assert rates_for("opus-4-7")["input_per_mtok"] == 5.00
    assert rates_for("opus")["input_per_mtok"] == 5.00
    assert rates_for("sonnet")["input_per_mtok"] == 3.00
    assert rates_for("haiku")["input_per_mtok"] == 1.00


def test_rates_for_strips_provider_prefix():
    """``anthropic/claude-opus-4-7`` (LiteLLM/SDK prefix) resolves."""
    assert rates_for("anthropic/claude-opus-4-7")["input_per_mtok"] == 5.00
    assert rates_for("us.anthropic.claude-opus-4-7")["input_per_mtok"] == 5.00


def test_rates_for_free_model_returns_zero():
    """Local inference (thinking's Hermes) prices at $0."""
    r = rates_for("hermes")
    assert r["input_per_mtok"] == 0.0
    assert r["output_per_mtok"] == 0.0
    assert is_free("hermes")
    assert is_free("local")
    assert not is_free("claude-opus-4-7")


def test_rates_for_unknown_raises():
    with pytest.raises(KeyError):
        rates_for("gpt-5")


def test_rates_dict_has_four_fields_per_entry():
    """Every entry must carry all four rate categories so cost math never KeyErrors."""
    for model, rates in RATES.items():
        for field in (
            "input_per_mtok",
            "output_per_mtok",
            "cache_write_per_mtok",
            "cache_read_per_mtok",
        ):
            assert field in rates, f"{model} missing {field}"
            assert rates[field] >= 0


# ---------------------------------------------------------------- estimate_tokens


def test_estimate_tokens_empty_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_rounds_up():
    # 1 char → 1 token (round-up branch).
    assert estimate_tokens("x") == 1
    # 4 chars → 1 token (no rounding).
    assert estimate_tokens("xxxx") == 1
    # 5 chars → 2 tokens (round-up).
    assert estimate_tokens("xxxxx") == 2


def test_estimate_tokens_long_prompt():
    text = "a" * 40_000
    # 40000 / 4 = 10000 tokens.
    assert estimate_tokens(text) == 10_000


# ---------------------------------------------------------------- estimate_cost


def test_estimate_cost_opus_known_case():
    # 10K prompt tokens + 8K output on Opus 4.7:
    # input:  10_000 / 1M * $5 = $0.05
    # output:  8_000 / 1M * $25 = $0.20
    # total:  $0.25
    cost = estimate_cost("claude-opus-4-7", 10_000, 8_000)
    assert cost == pytest.approx(0.25, rel=1e-9)


def test_estimate_cost_default_output_budget_is_8k():
    """Default output budget is 8K — matches the dispatcher's spec."""
    cost = estimate_cost("claude-opus-4-7", 1_000)
    # 1_000/1M * 5 + 8_000/1M * 25 = 0.005 + 0.20 = 0.205
    assert cost == pytest.approx(0.205, rel=1e-9)


def test_estimate_cost_sonnet():
    cost = estimate_cost("claude-sonnet-4-6", 20_000, 10_000)
    # 20_000/1M * 3 + 10_000/1M * 15 = 0.06 + 0.15 = 0.21
    assert cost == pytest.approx(0.21, rel=1e-9)


def test_estimate_cost_free_model_is_zero():
    assert estimate_cost("hermes", 100_000, 50_000) == 0.0


def test_estimate_cost_negative_tokens_coerced_to_zero():
    """A buggy caller doesn't produce a negative cost."""
    assert estimate_cost("claude-opus-4-7", -500, -200) == 0.0


def test_estimate_cost_accepts_custom_rates_mapping():
    """Caller can inject a rates dict (useful for new / unknown models)."""
    rates = {
        "input_per_mtok": 10.0,
        "output_per_mtok": 50.0,
        "cache_write_per_mtok": 12.5,
        "cache_read_per_mtok": 1.0,
    }
    cost = estimate_cost("totally-made-up", 1_000_000, 0, rates=rates)
    assert cost == pytest.approx(10.0, rel=1e-9)


# ---------------------------------------------------------------- format_usd


def test_format_usd_zero():
    assert format_usd(0) == "$0.00"


def test_format_usd_normal():
    assert format_usd(0.42) == "$0.42"
    assert format_usd(1.234) == "$1.23"
    assert format_usd(42) == "$42.00"


def test_format_usd_sub_cent_shows_sig_figs():
    # Below $0.01 — show four significant figures.
    rendered = format_usd(0.001234)
    assert rendered.startswith("$0.00")
    assert "1234" in rendered


def test_format_usd_negative():
    assert format_usd(-0.42) == "-$0.42"


# ---------------------------------------------------------------- read_worker_tokens


def test_read_worker_tokens_result_json_sdk_envelope(tmp_path):
    """SDK envelope with ``usage`` nested under a stringified ``result``."""
    scratch = tmp_path / "bug-7"
    scratch.mkdir()
    envelope = {
        "type": "result",
        "subtype": "success",
        "model": "claude-opus-4-7",
        "usage": {
            "input_tokens": 12_345,
            "output_tokens": 6_789,
            "cache_read_input_tokens": 1_000,
            "cache_creation_input_tokens": 500,
        },
        "result": json.dumps({"pr_url": "https://github.com/x/y/pull/1"}),
    }
    (scratch / "result.json").write_text(json.dumps(envelope))
    tokens = read_worker_tokens(scratch)
    assert tokens is not None
    assert tokens["input_tokens"] == 12_345
    assert tokens["output_tokens"] == 6_789
    assert tokens["cache_read_tokens"] == 1_000
    assert tokens["cache_write_tokens"] == 500
    assert tokens["model"] == "claude-opus-4-7"


def test_read_worker_tokens_result_json_bare_usage(tmp_path):
    """A worker that emits ``{"usage": {...}, "model": ...}`` top-level."""
    scratch = tmp_path / "bug-8"
    scratch.mkdir()
    (scratch / "result.json").write_text(
        json.dumps(
            {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 1_000, "output_tokens": 500},
            }
        )
    )
    tokens = read_worker_tokens(scratch)
    assert tokens["input_tokens"] == 1_000
    assert tokens["output_tokens"] == 500
    assert tokens["cache_read_tokens"] == 0
    assert tokens["cache_write_tokens"] == 0


def test_read_worker_tokens_falls_back_to_stderr_regex(tmp_path):
    """No result.json → scan stderr for ``Tokens used: input=X output=Y``."""
    scratch = tmp_path / "bug-9"
    scratch.mkdir()
    # result.json present but empty.
    (scratch / "result.json").write_text("")
    (scratch / "stderr.log").write_text(
        "chatter chatter\n"
        "Tokens used: input=4321 output=1234 model=claude-opus-4-7\n"
        "more chatter\n"
    )
    tokens = read_worker_tokens(scratch)
    assert tokens is not None
    assert tokens["input_tokens"] == 4_321
    assert tokens["output_tokens"] == 1_234


def test_read_worker_tokens_stderr_loose_sdk_verbose(tmp_path):
    """SDK verbose mode dumps ``input_tokens: NNN`` on separate lines."""
    scratch = tmp_path / "bug-10"
    scratch.mkdir()
    (scratch / "stderr.log").write_text(
        "{\n"
        '  "input_tokens": 2000,\n'
        '  "output_tokens": 800,\n'
        '  "cache_read_input_tokens": 100,\n'
        '  "cache_creation_input_tokens": 50,\n'
        '  "model": "anthropic/claude-opus-4-7"\n'
        "}\n"
    )
    tokens = read_worker_tokens(scratch)
    assert tokens is not None
    assert tokens["input_tokens"] == 2_000
    assert tokens["output_tokens"] == 800
    assert tokens["cache_read_tokens"] == 100
    assert tokens["cache_write_tokens"] == 50


def test_read_worker_tokens_returns_none_when_nothing_parseable(tmp_path):
    """Empty scratch dir → ``None``; dispatcher writes ``actual_usd: null``."""
    scratch = tmp_path / "bug-11"
    scratch.mkdir()
    (scratch / "result.json").write_text("")
    (scratch / "stderr.log").write_text("no numbers here at all\n")
    assert read_worker_tokens(scratch) is None


def test_read_worker_tokens_handles_missing_files(tmp_path):
    scratch = tmp_path / "bug-12"
    scratch.mkdir()
    # Neither file exists.
    assert read_worker_tokens(scratch) is None


def test_read_worker_tokens_handles_malformed_result_json(tmp_path):
    """Malformed JSON falls through to the stderr regex rather than crashing."""
    scratch = tmp_path / "bug-13"
    scratch.mkdir()
    (scratch / "result.json").write_text("{this is not { valid json")
    (scratch / "stderr.log").write_text("Tokens used: input=50 output=25\n")
    tokens = read_worker_tokens(scratch)
    assert tokens is not None
    assert tokens["input_tokens"] == 50
    assert tokens["output_tokens"] == 25


# ---------------------------------------------------------------- actual_cost_from_tokens


def test_actual_cost_from_tokens_opus_basic():
    # 10k input + 8k output on Opus 4.7 → $0.25
    cost = actual_cost_from_tokens("claude-opus-4-7", 10_000, 8_000)
    assert cost == pytest.approx(0.25, rel=1e-9)


def test_actual_cost_from_tokens_includes_cache_write_and_read():
    # 1_000_000 cache_read tokens on Opus 4.7 → $0.50
    # 1_000_000 cache_write tokens on Opus 4.7 → $6.25
    cost = actual_cost_from_tokens(
        "claude-opus-4-7",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == pytest.approx(6.75, rel=1e-9)


def test_actual_cost_from_tokens_free_model_is_zero():
    assert (
        actual_cost_from_tokens(
            "hermes", 1_000_000, 1_000_000, cache_read_tokens=1_000_000
        )
        == 0.0
    )
