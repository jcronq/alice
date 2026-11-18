"""Unit tests for ``interpretShortcut`` in ``static/index.html``
(PR ux/overview-shortcuts-help).

The helper decides which action a key event should produce. Extracted
by text slice and exercised under node, same pattern as
``test_status_site_reducer_js.py`` and ``test_status_site_tablist_js.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_interpret_source(html: str) -> str:
    """Pull just the ``interpretShortcut`` function out of index.html."""
    start = html.index("function interpretShortcut(")
    end = html.index("\n// List of shortcut rows", start)
    return html[start:end]


SCRIPT = r"""
%(helper)s
const ev = (over) => ({
  key: "", shiftKey: false, ctrlKey: false, metaKey: false, altKey: false,
  targetTagName: "body", isContentEditable: false,
  ...over,
});
const out = {};
// ── Help modal open / close ──────────────────────────────────────
out.question = interpretShortcut(ev({key: "?"}), {helpOpen: false, chord: null});
out.shiftSlash = interpretShortcut(ev({key: "/", shiftKey: true}), {helpOpen: false, chord: null});
out.escCloses = interpretShortcut(ev({key: "Escape"}), {helpOpen: true, chord: null});
out.escWhenClosed = interpretShortcut(ev({key: "Escape"}), {helpOpen: false, chord: null});
out.openSuppressesOthers = interpretShortcut(ev({key: "g"}), {helpOpen: true, chord: null});

// ── g-chord navigation ───────────────────────────────────────────
out.gStarts = interpretShortcut(ev({key: "g"}), {helpOpen: false, chord: null});
out.gThenO = interpretShortcut(ev({key: "o"}), {helpOpen: false, chord: "g"});
out.gThenC = interpretShortcut(ev({key: "c"}), {helpOpen: false, chord: "g"});
out.gThenB = interpretShortcut(ev({key: "b"}), {helpOpen: false, chord: "g"});
out.gThenX = interpretShortcut(ev({key: "x"}), {helpOpen: false, chord: "g"});
// Bare "o" with no chord does nothing.
out.bareO = interpretShortcut(ev({key: "o"}), {helpOpen: false, chord: null});

// ── Guards: inputs ───────────────────────────────────────────────
out.inInput = interpretShortcut(ev({key: "?", targetTagName: "input"}),
                                {helpOpen: false, chord: null});
out.inTextarea = interpretShortcut(ev({key: "g", targetTagName: "textarea"}),
                                   {helpOpen: false, chord: null});
out.inSelect = interpretShortcut(ev({key: "g", targetTagName: "select"}),
                                  {helpOpen: false, chord: null});
out.inContentEditable = interpretShortcut(ev({key: "?", isContentEditable: true}),
                                          {helpOpen: false, chord: null});
// Esc in an input still closes the modal (the escape hatch).
out.escInInputClosesModal = interpretShortcut(
  ev({key: "Escape", targetTagName: "input"}),
  {helpOpen: true, chord: null});

// ── Guards: modifier keys ────────────────────────────────────────
out.ctrlQuestion = interpretShortcut(ev({key: "?", ctrlKey: true}),
                                     {helpOpen: false, chord: null});
out.metaG = interpretShortcut(ev({key: "g", metaKey: true}),
                              {helpOpen: false, chord: null});
out.altO = interpretShortcut(ev({key: "o", altKey: true}),
                             {helpOpen: false, chord: "g"});
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    html = (STATIC_DIR / "index.html").read_text()
    helper_src = _extract_interpret_source(html)
    script = tmp_path_factory.mktemp("node") / "shortcuts.mjs"
    script.write_text(SCRIPT % {"helper": helper_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_question_mark_opens_help(result):
    assert result["question"] == {"type": "help"}
    # Shift+/ is what browsers typically send on US keyboards.
    assert result["shiftSlash"] == {"type": "help"}


def test_escape_closes_only_when_open(result):
    assert result["escCloses"] == {"type": "close"}
    assert result["escWhenClosed"] is None
    # Nothing else fires while help is open.
    assert result["openSuppressesOthers"] is None


def test_g_chord_navigation(result):
    assert result["gStarts"] == {"type": "chord", "key": "g"}
    assert result["gThenO"] == {"type": "nav", "slug": "overview"}
    assert result["gThenC"] == {"type": "nav", "slug": "chat"}
    assert result["gThenB"] == {"type": "nav", "slug": "bugs"}
    # Any other second-key cancels the chord without firing nav.
    assert result["gThenX"] == {"type": "chord", "key": None}
    # Bare second-letter does nothing on its own.
    assert result["bareO"] is None


def test_inputs_suppress_shortcuts(result):
    assert result["inInput"] is None
    assert result["inTextarea"] is None
    assert result["inSelect"] is None
    assert result["inContentEditable"] is None
    # Esc remains the escape hatch — closes modal even in an input.
    assert result["escInInputClosesModal"] == {"type": "close"}


def test_modifiers_suppress_shortcuts(result):
    assert result["ctrlQuestion"] is None
    assert result["metaG"] is None
    assert result["altO"] is None
