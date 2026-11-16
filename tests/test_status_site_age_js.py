"""Unit tests for the age/stamp formatters in ``static/index.html`` (slice-1 #881).

Jason wanted "N days" past 24 hours instead of "72h". The three helpers
``stampText``, ``ageString`` and ``ageStringSeconds`` live inline in the
single ``<script type="module">`` block in ``index.html``; we extract
them by text slice and exercise them under node, matching the pattern
used by ``test_status_site_reducer_js.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _extract_age_source(html: str) -> str:
    """Pull the three age helpers out of index.html.

    Returns a self-contained JS source string (plus the shared
    ``parseTs`` helper they lean on) that can run under node.
    """
    start = html.index("function parseTs(iso)")
    end = html.index("// Re-tint freshness tiles")
    return html[start:end]


SCRIPT = r"""
%(helpers)s
const out = {};

// ── stampText ────────────────────────────────────────────────────
// Reference "now" so results are deterministic.
const now = 1_700_000_000_000;
out.noTs = stampText(null, now);
out.sec = stampText(now - 42 * 1000, now);
out.min = stampText(now - 17 * 60 * 1000, now);
out.hr = stampText(now - (5 * 3600 + 7 * 60) * 1000, now);
out.hrFlat = stampText(now - 2 * 3600 * 1000, now);
out.hrEdge23 = stampText(now - 23 * 3600 * 1000, now);
out.day1 = stampText(now - 24 * 3600 * 1000, now);
out.day3 = stampText(now - 72 * 3600 * 1000, now);
out.day10 = stampText(now - 10 * 86400 * 1000, now);

// ── ageString ────────────────────────────────────────────────────
out.ageSec = ageString(new Date(now - 30 * 1000).toISOString(), now);
out.ageMin = ageString(new Date(now - 45 * 60 * 1000).toISOString(), now);
out.ageHr = ageString(new Date(now - (3 * 3600 + 15 * 60) * 1000).toISOString(), now);
out.ageDay1 = ageString(new Date(now - 24 * 3600 * 1000).toISOString(), now);
out.ageDay3 = ageString(new Date(now - 72 * 3600 * 1000).toISOString(), now);
out.ageDay30 = ageString(new Date(now - 30 * 86400 * 1000).toISOString(), now);
out.ageEdge23 = ageString(new Date(now - 23 * 3600 * 1000).toISOString(), now);
out.ageMissing = ageString(null, now);

// ── ageStringSeconds ─────────────────────────────────────────────
out.ageSsSec = ageStringSeconds(10);
out.ageSsMin = ageStringSeconds(600);
out.ageSsHr = ageStringSeconds(2 * 3600);
out.ageSsHrEdge23 = ageStringSeconds(23 * 3600);
out.ageSsDay1 = ageStringSeconds(24 * 3600);
out.ageSsDay3 = ageStringSeconds(72 * 3600);
out.ageSsDay7 = ageStringSeconds(7 * 86400 + 12 * 3600);  // 7d (floor)

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    html = (STATIC_DIR / "index.html").read_text()
    helpers_src = _extract_age_source(html)
    script = tmp_path_factory.mktemp("node") / "age.mjs"
    script.write_text(SCRIPT % {"helpers": helpers_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_stamp_text_sub_day_unchanged(result):
    """Nothing under 24h changed; only 24h+ got the new day format."""
    assert result["noTs"] == "no update yet"
    assert result["sec"] == "updated 42s ago"
    assert result["min"] == "updated 17m ago"
    assert result["hr"] == "updated 5h 7m ago"
    assert result["hrFlat"] == "updated 2h ago"
    assert result["hrEdge23"] == "updated 23h ago"


def test_stamp_text_day_boundary(result):
    """24h, 72h and 10d should render as days."""
    assert result["day1"] == "updated 1d ago"
    assert result["day3"] == "updated 3d ago"
    assert result["day10"] == "updated 10d ago"


def test_age_string_sub_day_unchanged(result):
    assert result["ageSec"] == "30s"
    assert result["ageMin"] == "45m"
    assert result["ageHr"] == "3h 15m"
    assert result["ageEdge23"] == "23h"
    assert result["ageMissing"] == "—"


def test_age_string_day_boundary(result):
    assert result["ageDay1"] == "1d"
    assert result["ageDay3"] == "3d"
    assert result["ageDay30"] == "30d"


def test_age_string_seconds_sub_day_unchanged(result):
    assert result["ageSsSec"] == "10s"
    assert result["ageSsMin"] == "10m"
    assert result["ageSsHr"] == "2h"
    assert result["ageSsHrEdge23"] == "23h"


def test_age_string_seconds_day_boundary(result):
    assert result["ageSsDay1"] == "1d"
    assert result["ageSsDay3"] == "3d"
    # 7d 12h floors to 7d.
    assert result["ageSsDay7"] == "7d"
