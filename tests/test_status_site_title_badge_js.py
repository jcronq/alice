"""Unit tests for ``waitingOnYouCount`` + ``titleWithBadge`` in
``static/index.html`` (PR ux/overview-title-badge).

The two helpers decide the Waiting-on-you count the shell mirrors
into ``document.title``. Extracted by text slice and exercised under
node (same pattern as the sibling ``test_status_site_reducer_js.py``).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_helpers_source(html: str) -> str:
    """Pull waitingOnYouCount + titleWithBadge out of index.html."""
    start = html.index("function waitingOnYouCount(")
    end = html.index("\n// A tier groups", start)
    return html[start:end]


SCRIPT = r"""
%(helpers)s
const out = {};
// ── waitingOnYouCount ──────────────────────────────────────────────
out.emptySection = waitingOnYouCount({});
out.nullish = waitingOnYouCount(null);
out.undef = waitingOnYouCount(undefined);
// Each source counts 1:1.
out.prsOnly = waitingOnYouCount({prs: [{n: 1}, {n: 2}]});
out.designOnly = waitingOnYouCount({design_review: [{}, {}, {}]});
out.attnOnly = waitingOnYouCount({attention_reacts: [{}]});
out.rowsOnly = waitingOnYouCount({rows: [{}, {}]});
// Sum across sources.
out.mixed = waitingOnYouCount({
  prs: [{n: 1}], design_review: [{}, {}],
  attention_reacts: [{}], rows: [{}, {}, {}],
});

// ── titleWithBadge ─────────────────────────────────────────────────
out.zero = titleWithBadge("Alice — status", 0);
out.one = titleWithBadge("Alice — status", 1);
out.many = titleWithBadge("Alice — status", 42);
// Negative / NaN guards — count must be a sane positive int to prepend.
out.negative = titleWithBadge("Alice — status", -1);
out.nanCount = titleWithBadge("Alice — status", NaN);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    html = (STATIC_DIR / "index.html").read_text()
    helpers_src = _extract_helpers_source(html)
    script = tmp_path_factory.mktemp("node") / "title.mjs"
    script.write_text(SCRIPT % {"helpers": helpers_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_waiting_count_on_empty_or_missing(result):
    assert result["emptySection"] == 0
    assert result["nullish"] == 0
    assert result["undef"] == 0


def test_waiting_count_sums_each_source(result):
    assert result["prsOnly"] == 2
    assert result["designOnly"] == 3
    assert result["attnOnly"] == 1
    assert result["rowsOnly"] == 2
    assert result["mixed"] == 1 + 2 + 1 + 3


def test_title_with_badge_hides_when_zero(result):
    assert result["zero"] == "Alice — status"


def test_title_with_badge_prepends_count(result):
    assert result["one"] == "(1) Alice — status"
    assert result["many"] == "(42) Alice — status"


def test_title_with_badge_guards_bad_counts(result):
    # Negative and NaN both fall through the ``n > 0`` gate.
    assert result["negative"] == "Alice — status"
    assert result["nanCount"] == "Alice — status"
