"""metrics — stable, tested implementations of vault-health metrics.

The thinking prompt used to compute these via inline bash, which produced
order-of-magnitude wrong numbers and drifted between wakes. This package
holds the algorithms in Python with regression tests so the numbers are
reproducible across calls.

See ``metrics.vault_health`` for the metric implementations and
``alice/tests/test_vault_health.py`` for the eval contracts (each metric
has a buggy-baseline assertion + a fixed-implementation assertion in the
same test).
"""

from __future__ import annotations

__all__ = ["vault_health"]
