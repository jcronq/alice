"""Freshness tile — the small per-section "updated X ago" pill.

Emits an HTML span the client can refresh in place: ``data-age``
carries the age in seconds at render time, a trailing script in
``static/index.html`` updates both the attribute and the label on
each poll. CSS tints the pill green / amber / red from the
``data-fresh`` state so a stale section is visible at a glance.

Three freshness states, following the agentry-dashboard model
(``overview.py``'s fresh_state precedence):

* ``ok``    — last success within ``amber_multiple × poll_interval``
* ``amber`` — within ``red_multiple × poll_interval``
* ``red``   — beyond ``red_multiple`` (stale), *or* the last poll
              failed
* ``owed``  — the poller hasn't succeeded since startup

``amber_multiple`` and ``red_multiple`` default to 2 and 10
respectively; thresholds are multiples of each section's own poll
cadence (running-now 15 s, gh-backed 300 s), not the dashboard's
60 / 360 minutes — our poll cadences are 100× tighter.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_AMBER_MULTIPLE = 2.0
DEFAULT_RED_MULTIPLE = 10.0

# Future-skew tolerance (seconds): a last_rendered timestamp this far
# ahead of ``now`` is treated as clock skew → amber, not ok.
DEFAULT_FUTURE_SKEW_SECONDS = 300.0

FreshState = str  # one of "ok" | "amber" | "red" | "owed"


@dataclass(frozen=True)
class FreshnessParams:
    """Per-section thresholds. Default values match the dashboard's
    ``fresh_state`` grammar; override per source if a renderer wants
    tighter or looser bounds."""

    poll_interval_seconds: float
    amber_multiple: float = DEFAULT_AMBER_MULTIPLE
    red_multiple: float = DEFAULT_RED_MULTIPLE
    future_skew_seconds: float = DEFAULT_FUTURE_SKEW_SECONDS


def compute_state(
    age_seconds: float,
    params: FreshnessParams,
    *,
    failed: bool = False,
    owed: bool = False,
) -> FreshState:
    """Return the freshness state for one section.

    Short-circuits:

    * ``owed=True`` → ``"owed"`` (first-pending; never polled)
    * ``failed=True`` → ``"red"`` (last poll explicitly failed)
    * ``age_seconds < -future_skew_seconds`` → ``"amber"`` (clock skew)

    Otherwise picks the band from ``age_seconds`` /
    ``poll_interval_seconds``.
    """
    if owed:
        return "owed"
    if failed:
        return "red"
    # Clock skew: a last_rendered noticeably in the future is a bug
    # downstream, not freshness. Flag it (amber) rather than claiming
    # "ok" from a nonsense delta.
    if age_seconds < -params.future_skew_seconds:
        return "amber"
    age = max(0.0, age_seconds)
    amber_at = params.amber_multiple * params.poll_interval_seconds
    red_at = params.red_multiple * params.poll_interval_seconds
    if age < amber_at:
        return "ok"
    if age < red_at:
        return "amber"
    return "red"


def _format_age(seconds: float) -> str:
    """Human age label for the tile: ``just now`` / ``5 sec`` / ``3 min``
    / ``2 h 4 min`` / ``5 d``.

    Negative inputs (clock-skewed source) are clamped to ``just now``.
    """
    s = int(max(0.0, seconds))
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s} sec ago"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m} min ago"
    h, rem_m = divmod(m, 60)
    if h < 24:
        return f"{h} h {rem_m} min ago" if rem_m else f"{h} h ago"
    d, rem_h = divmod(h, 24)
    return f"{d} d {rem_h} h ago" if rem_h else f"{d} d ago"


def tile_html(
    age_seconds: float,
    params: FreshnessParams,
    *,
    failed: bool = False,
    owed: bool = False,
    source: str = "",
) -> str:
    """Return the ``<span class="fx-pill freshness-tile" ...>...</span>``.

    ``source`` is dropped into the ``data-source`` attribute (used by
    the client refresh to retarget the right tile when multiple
    sections render on the same page).
    """
    state = compute_state(age_seconds, params, failed=failed, owed=owed)
    if owed:
        label = "owed"
    else:
        label = _format_age(age_seconds)
        if failed:
            label = f"poll failed · {label}"
    age_attr = int(max(0.0, age_seconds))
    src_attr = f' data-source="{source}"' if source else ""
    return (
        f'<span class="fx-pill freshness-tile" data-age="{age_attr}" '
        f'data-fresh="{state}"{src_attr}>{label}</span>'
    )
