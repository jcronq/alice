"""Convert pi-coding-agent's usage dict to :class:`UsageInfo`.

Pi reports usage as ``{"input": int, "output": int, "cacheRead":
int|None, "cacheWrite": int|None, "totalTokens": int|None,
"cost": {...}}``. UsageInfo uses Anthropic's wire field names
(``input_tokens``, ``output_tokens``, etc.) because the
event-log + viewer aggregators were built around that shape.

Pi's ``cost.total`` is API-rate USD — IGNORED for subscription
billing. :class:`KernelResult.cost_usd` is set to ``None`` for
pi-routed turns; viewer renders these as "subscription-billed".
"""

from __future__ import annotations

from typing import Optional

from alice_core.kernel import UsageInfo


__all__ = ["pi_usage_to_info"]


def pi_usage_to_info(raw: Optional[dict]) -> Optional[UsageInfo]:
    if not raw or not isinstance(raw, dict):
        return None
    return UsageInfo(
        input_tokens=int(raw.get("input") or 0),
        output_tokens=int(raw.get("output") or 0),
        cache_read_input_tokens=_int_or_none(raw.get("cacheRead")),
        cache_creation_input_tokens=_int_or_none(raw.get("cacheWrite")),
        total_tokens=_int_or_none(raw.get("totalTokens")),
    )


def _int_or_none(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
