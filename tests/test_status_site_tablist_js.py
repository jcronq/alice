"""Unit tests for the ``nextTabIndex`` helper in ``static/index.html``
(PR ux/overview-tablist-keys).

The helper drives Left / Right / Home / End navigation in the ARIA
tablist pattern. We extract it by text slice and exercise it under
node, matching ``test_status_site_reducer_js.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_next_tab_index_source(html: str) -> str:
    """Pull just the ``nextTabIndex`` function out of index.html."""
    start = html.index("function nextTabIndex(")
    # The function ends at the next blank line + ``function`` declaration.
    end = html.index("\nfunction ", start + 1)
    return html[start:end]


SCRIPT = r"""
%(helper)s
const out = {};
// 4-tab setup (Overview, Chat, Bugs, Resources — the typical header).
out.right_from_0 = nextTabIndex(0, "ArrowRight", 4);
out.right_from_3_wraps = nextTabIndex(3, "ArrowRight", 4);
out.left_from_0_wraps = nextTabIndex(0, "ArrowLeft", 4);
out.left_from_2 = nextTabIndex(2, "ArrowLeft", 4);
out.home_from_mid = nextTabIndex(2, "Home", 4);
out.end_from_mid = nextTabIndex(1, "End", 4);
out.other_key_noop = nextTabIndex(2, "Enter", 4);
out.unknown_key_noop = nextTabIndex(1, "x", 4);
// Edge cases.
out.empty_zero = nextTabIndex(0, "ArrowRight", 0);
out.single_right = nextTabIndex(0, "ArrowRight", 1);
out.single_left = nextTabIndex(0, "ArrowLeft", 1);
out.single_home = nextTabIndex(0, "Home", 1);
out.single_end = nextTabIndex(0, "End", 1);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    html = (STATIC_DIR / "index.html").read_text()
    helper_src = _extract_next_tab_index_source(html)
    script = tmp_path_factory.mktemp("node") / "tablist.mjs"
    script.write_text(SCRIPT % {"helper": helper_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_arrow_right_advances_and_wraps(result):
    assert result["right_from_0"] == 1
    assert result["right_from_3_wraps"] == 0


def test_arrow_left_retreats_and_wraps(result):
    assert result["left_from_2"] == 1
    assert result["left_from_0_wraps"] == 3


def test_home_jumps_to_first(result):
    assert result["home_from_mid"] == 0


def test_end_jumps_to_last(result):
    assert result["end_from_mid"] == 3


def test_other_keys_do_not_move(result):
    assert result["other_key_noop"] == 2
    assert result["unknown_key_noop"] == 1


def test_edge_cases(result):
    # Zero-tab list is defensive: return 0, not NaN.
    assert result["empty_zero"] == 0
    # Single-tab list: every key stays put.
    assert result["single_right"] == 0
    assert result["single_left"] == 0
    assert result["single_home"] == 0
    assert result["single_end"] == 0
