"""Alice API-cost pricing — dollar estimates + actuals for Claude worker calls.

Four public helpers (import whichever you need):

    from alice_pricing import (
        estimate_tokens,       # 4-chars/token heuristic
        estimate_cost,         # $ for a prompt + output budget
        format_usd,            # pretty-print a USD float
        read_worker_tokens,    # scan a worker scratch dir for token counts
        actual_cost_from_tokens,  # $ from measured token usage
        rates_for,             # look up a model's rate dict (dollars per MTok)
        PRICING_SOURCE_DATE,   # when the rate table was pulled
    )

The rate table lives in ``alice_pricing.rates`` as a plain Python dict so
Jason can edit it without reaching for a schema.
"""

from __future__ import annotations

from .actuals import actual_cost_from_tokens, read_worker_tokens
from .estimate import estimate_cost, estimate_tokens, format_usd
from .rates import PRICING_SOURCE_DATE, RATES, rates_for

__all__ = [
    "RATES",
    "PRICING_SOURCE_DATE",
    "rates_for",
    "estimate_tokens",
    "estimate_cost",
    "format_usd",
    "read_worker_tokens",
    "actual_cost_from_tokens",
]
