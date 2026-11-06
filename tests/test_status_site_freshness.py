"""Tests for the freshness tile (``status_site/freshness.py``).

The tile is a small HTML span the client's refresh loop re-tints
every few seconds; the server emits the initial state. Covered:

* state bands (ok / amber / red) by age vs. poll interval
* owed shortcut (poller has never populated)
* failed-poll shortcut (red regardless of age)
* clock-skew tolerance
* HTML shape so the client's JS can find + update the pill
"""

from __future__ import annotations

from status_site.freshness import (
    DEFAULT_AMBER_MULTIPLE,
    DEFAULT_RED_MULTIPLE,
    FreshnessParams,
    compute_state,
    tile_html,
)


def _params(interval: float = 60.0) -> FreshnessParams:
    return FreshnessParams(poll_interval_seconds=interval)


# ─── compute_state ─────────────────────────────────────────────────


def test_fresh_within_amber_multiple_is_ok():
    # poll = 60s, amber at 2x = 120s; 59s < 120s → ok.
    assert compute_state(59, _params(60.0)) == "ok"


def test_between_amber_and_red_multiples_is_amber():
    # poll = 60s, 2x = 120, 10x = 600; 300s is between → amber.
    assert compute_state(300, _params(60.0)) == "amber"


def test_beyond_red_multiple_is_red():
    assert compute_state(601, _params(60.0)) == "red"


def test_defaults_match_dashboard_two_and_ten():
    # Sanity — document the published default multipliers.
    assert DEFAULT_AMBER_MULTIPLE == 2.0
    assert DEFAULT_RED_MULTIPLE == 10.0


def test_owed_short_circuits_regardless_of_age():
    assert compute_state(9999, _params(60.0), owed=True) == "owed"
    assert compute_state(0, _params(60.0), owed=True) == "owed"


def test_failed_poll_is_red_regardless_of_age():
    assert compute_state(0, _params(60.0), failed=True) == "red"


def test_future_skew_within_tolerance_is_ok():
    # A tiny future delta from clock skew counts as "just now".
    assert compute_state(-60, _params(60.0)) == "ok"


def test_future_skew_beyond_tolerance_goes_amber():
    # 400s in the future is past the 300s default skew tolerance.
    assert compute_state(-400, _params(60.0)) == "amber"


def test_short_poll_interval_bands_are_tight():
    # Running-now polls every 15s. Amber at 30s, red at 150s.
    p = _params(15.0)
    assert compute_state(29, p) == "ok"
    assert compute_state(31, p) == "amber"
    assert compute_state(151, p) == "red"


def test_slow_poll_interval_bands_are_wider():
    # gh-backed sources poll every 300s. Amber at 600s, red at 3000s.
    p = _params(300.0)
    assert compute_state(500, p) == "ok"
    assert compute_state(700, p) == "amber"
    assert compute_state(4000, p) == "red"


# ─── tile_html: shape ──────────────────────────────────────────────


def test_tile_html_carries_age_and_fresh_attributes():
    html = tile_html(30.0, _params(60.0))
    assert 'class="fx-pill freshness-tile"' in html
    assert 'data-age="30"' in html
    assert 'data-fresh="ok"' in html


def test_tile_html_source_data_attribute():
    html = tile_html(30.0, _params(60.0), source="running_now")
    assert 'data-source="running_now"' in html


def test_tile_html_owed_label_and_state():
    html = tile_html(0.0, _params(60.0), owed=True)
    assert 'data-fresh="owed"' in html
    assert ">owed</span>" in html


def test_tile_html_failed_label_prepends_marker():
    html = tile_html(30.0, _params(60.0), failed=True)
    assert 'data-fresh="red"' in html
    assert "poll failed" in html


def test_tile_html_human_age_format():
    # Rough label coverage — the client refreshes these, but the
    # server-side initial render should also read well.
    assert "just now" in tile_html(0, _params(60.0))
    assert "30 sec ago" in tile_html(30, _params(60.0))
    assert "5 min ago" in tile_html(5 * 60, _params(300.0))
    # Hours with remainder minutes.
    assert "1 h 2 min ago" in tile_html(3720, _params(300.0))
    # Days.
    assert "2 d ago" in tile_html(2 * 86400, _params(300.0))
