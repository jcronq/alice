"""Unit tests for the SSE snapshot reducer in ``static/index.html`` (#858).

The reducer + ``sectionIsMeaningful`` helper live inline in the single
``<script type="module">`` block in ``index.html``. We extract them by
text slice and exercise them under node, matching the pattern used by
``test_status_site_chat_ux_js.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_reducer_source(html: str) -> str:
    """Pull the ``sectionIsMeaningful`` + ``reducer`` block out of index.html.

    Returns a self-contained JS source string that can run under node.
    """
    # Start at the ``sectionIsMeaningful`` declaration; stop at the next
    # top-level helper the module defines. The reducer lives between
    # ``function sectionIsMeaningful`` and ``// ── 5. event-stream hook``.
    start = html.index("function sectionIsMeaningful(")
    end = html.index("// ── 5. event-stream hook")
    return html[start:end]


SCRIPT = r"""
%(reducer)s
const INITIAL = {sections: {}, ts: {}, connected: false, error: null};
const out = {};

// Shape of the full-pipeline snapshot the user already saw when they
// switched tabs.
const full_in_flight = {
  markdown: "8 in flight",
  pipeline: [
    {repo: "jcronq/alice", number: 858, state: "selected", title: "t"},
  ],
  pipeline_by_component: [{component: "status-site", count: 1, rows: []}],
  running: [],
  surfaces: [],
  tasks: [],
  pipeline_failed: [],
};
const empty_in_flight = {
  markdown: "*(nothing in flight)*",
  pipeline: [],
  pipeline_by_component: [],
  running: [],
  surfaces: [],
  tasks: [],
  pipeline_failed: [],
};

// ── Meaningfulness probe ──────────────────────────────────────────
out.full_meaningful = sectionIsMeaningful(full_in_flight);
out.empty_meaningful = sectionIsMeaningful(empty_in_flight);
out.undef_meaningful = sectionIsMeaningful(undefined);
out.null_meaningful = sectionIsMeaningful(null);
out.markdown_only = sectionIsMeaningful({markdown: "real text"});
out.markdown_sentinel = sectionIsMeaningful({markdown: "*(queue clear)*"});
out.rows_only = sectionIsMeaningful({rows: [{x: 1}]});
out.a2ui_only = sectionIsMeaningful({a2ui: [{envelope: 1}]});

// ── Scenario 1 — full snapshot after full snapshot replaces ──────
let s1 = reducer(INITIAL, {
  type: "snapshot",
  ts: 1000,
  sections: {in_flight: full_in_flight},
});
out.s1_pipeline_len = s1.sections.in_flight.pipeline.length;
out.s1_ts = s1.ts.in_flight;
out.s1_connected = s1.connected;

// ── Scenario 2 — transient-empty after full keeps prior ──────────
let s2 = reducer(s1, {
  type: "snapshot",
  ts: 2000,
  sections: {in_flight: empty_in_flight},
});
out.s2_pipeline_len = s2.sections.in_flight.pipeline.length;
out.s2_ts = s2.ts.in_flight;  // stays at 1000; prior kept
out.s2_connected = s2.connected;

// ── Scenario 3 — real refresh after keep accepts new full data ───
const refreshed = {
  ...full_in_flight,
  pipeline: [
    {repo: "jcronq/alice", number: 858, state: "building", title: "t"},
    {repo: "jcronq/alice", number: 859, state: "selected", title: "u"},
  ],
};
let s3 = reducer(s2, {
  type: "snapshot",
  ts: 3000,
  sections: {in_flight: refreshed},
});
out.s3_pipeline_len = s3.sections.in_flight.pipeline.length;
out.s3_ts = s3.ts.in_flight;

// ── Scenario 4 — first-ever snapshot with empty in_flight renders
// (not stuck forever); subsequent full snapshot replaces. ────────
let s4a = reducer(INITIAL, {
  type: "snapshot",
  ts: 100,
  sections: {in_flight: empty_in_flight},
});
out.s4a_pipeline_len = s4a.sections.in_flight.pipeline.length;
out.s4a_has_section = "in_flight" in s4a.sections;
out.s4a_ts = s4a.ts.in_flight;

let s4b = reducer(s4a, {
  type: "snapshot",
  ts: 200,
  sections: {in_flight: full_in_flight},
});
out.s4b_pipeline_len = s4b.sections.in_flight.pipeline.length;
out.s4b_ts = s4b.ts.in_flight;

// ── Scenario 5 — section.update path still replaces one section ──
let s5 = reducer(s3, {
  type: "section.update",
  name: "in_flight",
  ts: 4000,
  data: empty_in_flight,
});
out.s5_pipeline_len = s5.sections.in_flight.pipeline.length;
out.s5_ts = s5.ts.in_flight;

// ── Scenario 6 — partial transient-empty: one section stays, one
// updates. ──────────────────────────────────────────────────────
const prior = {
  type: "snapshot",
  ts: 500,
  sections: {
    in_flight: full_in_flight,
    waiting_on_you: {markdown: "ask Jason about X", rows: [{id: "a"}]},
  },
};
let s6_prior = reducer(INITIAL, prior);
let s6 = reducer(s6_prior, {
  type: "snapshot",
  ts: 600,
  sections: {
    in_flight: empty_in_flight,  // transient wipe — keep prior
    waiting_on_you: {markdown: "ask Jason about Y", rows: [{id: "b"}]},
  },
});
out.s6_in_flight_len = s6.sections.in_flight.pipeline.length;
out.s6_in_flight_ts = s6.ts.in_flight;  // kept at 500
out.s6_waiting_rows = s6.sections.waiting_on_you.rows.length;
out.s6_waiting_ts = s6.ts.waiting_on_you;  // bumped to 600

// ── Scenario 7 — genuine empty refresh after another genuine empty
// should still land (sentinel "nothing here" markdown is not
// meaningful, so empty-after-empty passes through). ─────────────
let s7_a = reducer(INITIAL, {
  type: "snapshot",
  ts: 10,
  sections: {in_flight: empty_in_flight},
});
let s7_b = reducer(s7_a, {
  type: "snapshot",
  ts: 20,
  sections: {in_flight: {...empty_in_flight, markdown: "*(nothing in flight)*"}},
});
out.s7_ts = s7_b.ts.in_flight;  // 20 — advanced

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    html = (STATIC_DIR / "index.html").read_text()
    reducer_src = _extract_reducer_source(html)
    script = tmp_path_factory.mktemp("node") / "reducer.mjs"
    script.write_text(SCRIPT % {"reducer": reducer_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_section_meaningfulness_probe(result):
    assert result["full_meaningful"] is True
    assert result["empty_meaningful"] is False
    assert result["undef_meaningful"] is False
    assert result["null_meaningful"] is False
    assert result["markdown_only"] is True
    assert result["markdown_sentinel"] is False
    assert result["rows_only"] is True
    assert result["a2ui_only"] is True


def test_full_snapshot_populates_state(result):
    assert result["s1_pipeline_len"] == 1
    assert result["s1_ts"] == 1000
    assert result["s1_connected"] is True


def test_transient_empty_after_full_keeps_prior(result):
    # This is the #858 regression — if the next snapshot arrives with
    # empty pipeline, the reducer must NOT wipe the prior full payload.
    assert result["s2_pipeline_len"] == 1, (
        "transient-empty snapshot wiped the known-good pipeline; "
        "reducer regression from issue #858"
    )
    assert result["s2_ts"] == 1000, "ts bumped even though we kept prior"
    assert result["s2_connected"] is True


def test_real_refresh_after_keep_still_lands(result):
    assert result["s3_pipeline_len"] == 2
    assert result["s3_ts"] == 3000


def test_first_ever_empty_snapshot_does_not_stick_forever(result):
    # First-ever empty is accepted (prior is undefined).
    assert result["s4a_pipeline_len"] == 0
    assert result["s4a_has_section"] is True
    assert result["s4a_ts"] == 100
    # Follow-up full snapshot replaces the empty cleanly.
    assert result["s4b_pipeline_len"] == 1
    assert result["s4b_ts"] == 200


def test_section_update_path_still_replaces(result):
    # section.update is the unicast path — not subject to the keep-prior
    # guard. If a renderer explicitly pushes an empty, it goes through.
    assert result["s5_pipeline_len"] == 0
    assert result["s5_ts"] == 4000


def test_partial_snapshot_keeps_one_updates_other(result):
    assert result["s6_in_flight_len"] == 1
    assert result["s6_in_flight_ts"] == 500
    assert result["s6_waiting_rows"] == 1
    assert result["s6_waiting_ts"] == 600


def test_empty_after_empty_still_advances_ts(result):
    # The nothing-here sentinel is not meaningful, so prior isn't
    # meaningful either — the reducer accepts the incoming empty and
    # bumps ts normally.
    assert result["s7_ts"] == 20
