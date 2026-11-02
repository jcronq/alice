"""Claude API pricing table — dollars per million tokens.

Rates pulled 2026-11-02 from Anthropic's own documentation:

    https://docs.anthropic.com/en/docs/about-claude/pricing
    https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
    https://www.anthropic.com/news/claude-opus-4-7
    https://www.anthropic.com/news/claude-opus-4-8

Keep this a flat Python literal. Jason wants to edit prices in-place
without wiring in schema validation; the only runtime shape check is
``rates_for``'s dict return.

Keys are the exact model ids Anthropic ships; aliases in ``_ALIASES``
map common short names (``opus-4-7``) to the canonical id. If a model
isn't in the table, ``rates_for`` raises ``KeyError`` — the caller
should decide whether to default or skip (pricing an unknown model
with made-up numbers is a worse failure than a loud miss).

Cache rates in the table are the 5-minute cache variant (Anthropic's
default); a 1-hour cache write is roughly ``cache_write_per_mtok * 1.6``.
``actual_cost_from_tokens`` surfaces both cache fields but defaults
to zero so a worker that didn't exercise caching stays honest.
"""

from __future__ import annotations

from typing import Mapping


PRICING_SOURCE_DATE = "2026-11-02"


# Rate rows: dollars per million tokens.
#   input_per_mtok          — regular (non-cached) input
#   output_per_mtok         — model output
#   cache_write_per_mtok    — 5-minute cache write (ephemeral=5m)
#   cache_read_per_mtok     — cache hit / refresh
RATES: dict[str, dict[str, float]] = {
    # --- Opus family -------------------------------------------------
    # Opus 4.7 (current flagship as of pricing-source-date): docs page
    # shows base rate $5 / $25, 5-min cache write $6.25, cache read $0.50.
    "claude-opus-4-7": {
        "input_per_mtok": 5.00,
        "output_per_mtok": 25.00,
        "cache_write_per_mtok": 6.25,
        "cache_read_per_mtok": 0.50,
    },
    # Opus 4.8 (announcement 2026): same base rate as 4.7 per the
    # Anthropic news post.
    "claude-opus-4-8": {
        "input_per_mtok": 5.00,
        "output_per_mtok": 25.00,
        "cache_write_per_mtok": 6.25,
        "cache_read_per_mtok": 0.50,
    },
    # Opus 4.6 — same post-reduction rate as 4.7.
    "claude-opus-4-6": {
        "input_per_mtok": 5.00,
        "output_per_mtok": 25.00,
        "cache_write_per_mtok": 6.25,
        "cache_read_per_mtok": 0.50,
    },
    # Opus 4 / 4.1 — pre-reduction (keep around for historical cost
    # audits of workers that ran before the rate cut).
    "claude-opus-4": {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_write_per_mtok": 18.75,
        "cache_read_per_mtok": 1.50,
    },
    "claude-opus-4-1": {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_write_per_mtok": 18.75,
        "cache_read_per_mtok": 1.50,
    },
    # --- Sonnet family -----------------------------------------------
    # Sonnet 4 / 4.6 — $3 / $15 input/output; cache_write $3.75,
    # cache_read $0.30 (standard Sonnet rate carried across sub-versions).
    "claude-sonnet-4": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "claude-sonnet-4-5": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "claude-sonnet-4-6": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    # --- Haiku family -----------------------------------------------
    # Haiku 4.5 — $1 / $5; cache_write $1.25, cache_read $0.10 (same
    # 1.25× / 0.1× multipliers Anthropic applies across the family).
    "claude-haiku-4-5": {
        "input_per_mtok": 1.00,
        "output_per_mtok": 5.00,
        "cache_write_per_mtok": 1.25,
        "cache_read_per_mtok": 0.10,
    },
    "claude-3-5-haiku": {
        "input_per_mtok": 0.80,
        "output_per_mtok": 4.00,
        "cache_write_per_mtok": 1.00,
        "cache_read_per_mtok": 0.08,
    },
}


# Short-name aliases Jason / workers might emit instead of the full id.
_ALIASES: dict[str, str] = {
    "opus-4-7": "claude-opus-4-7",
    "opus": "claude-opus-4-7",
    "opus-4-8": "claude-opus-4-8",
    "opus-4-6": "claude-opus-4-6",
    "opus-4": "claude-opus-4",
    "opus-4-1": "claude-opus-4-1",
    "sonnet": "claude-sonnet-4-6",
    "sonnet-4": "claude-sonnet-4",
    "sonnet-4-5": "claude-sonnet-4-5",
    "sonnet-4-6": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "haiku-4-5": "claude-haiku-4-5",
    "haiku-3-5": "claude-3-5-haiku",
}


# Models we deliberately price at zero: self-hosted / local inference
# (thinking's NousResearch Hermes on Bazzite is the current example).
# ``rates_for`` returns the zero dict for these; callers can detect
# a free model with ``rates_for(model)["input_per_mtok"] == 0``.
FREE_MODELS: frozenset[str] = frozenset(
    {
        "local",
        "local-hermes",
        "nous-hermes",
        "nousresearch-hermes",
        "hermes",
        "hermes-4",
        "hermes-70b",
        "qwen",
    }
)

_FREE_RATE: dict[str, float] = {
    "input_per_mtok": 0.0,
    "output_per_mtok": 0.0,
    "cache_write_per_mtok": 0.0,
    "cache_read_per_mtok": 0.0,
}


def _normalise(model: str) -> str:
    """Strip the ``anthropic/`` / ``bedrock-`` provider prefix the SDK adds."""
    s = (model or "").strip().lower()
    for prefix in ("anthropic/", "anthropic.", "bedrock/", "us.anthropic.", "eu.anthropic."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def rates_for(model: str) -> Mapping[str, float]:
    """Return the per-million-token rate dict for ``model``.

    Resolution order:
        1. canonical model id in ``RATES``
        2. alias in ``_ALIASES`` → ``RATES``
        3. ``FREE_MODELS`` → zero dict (local inference, no API cost)
        4. ``KeyError`` with a message naming the unknown model

    Returned dict is read-only semantics — callers must not mutate the
    shared mapping.
    """
    canonical = _normalise(model)
    if canonical in RATES:
        return RATES[canonical]
    aliased = _ALIASES.get(canonical)
    if aliased and aliased in RATES:
        return RATES[aliased]
    if canonical in FREE_MODELS:
        return _FREE_RATE
    raise KeyError(f"unknown model {model!r}; add it to alice_pricing.rates.RATES")


def is_free(model: str) -> bool:
    """True iff ``model`` resolves to a zero-rate (local inference) entry."""
    try:
        rates = rates_for(model)
    except KeyError:
        return False
    return rates.get("input_per_mtok", 0.0) == 0.0 and rates.get("output_per_mtok", 0.0) == 0.0
