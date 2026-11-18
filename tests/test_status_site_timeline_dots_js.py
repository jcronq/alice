"""Unit tests for the TimelineDots React component in ``static/index.html``.

The component lives inline in the single ``<script type="module">`` block
alongside the SSE shell. We extract the ``STAGE_LABELS`` table and the
``TimelineDots`` function by text slice, stub ``h()``/``useState``/``useEffect``
with trivial equivalents, and drive the render under node with a fake
``fetch`` that returns a fixture aggregate.

What we actually assert:

- Five dots render in the canonical stage order (design → merge).
- Each dot carries the ``status-<rollup>`` class from the server
  response.
- Hover title packs task_id, started_at, and the summary so the tooltip
  reads cleanly.
- An unknown slug (no stages) renders five grey dots + the empty hint.

Browser-DOM tests (jsdom) would catch more, but this matches how the
other ``test_status_site_*_js.py`` suites scope their coverage and keeps
the harness one file per feature, not a sprawling UI test bed.
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


def _extract_source(html: str) -> str:
    """Pull STAGE_LABELS + TimelineDots out of index.html.

    Returns a self-contained JS source string that the harness can
    source into a stub environment. We stop just before the next
    ``const EFFORT_SEGMENTS`` marker so no unrelated effort-picker
    code sneaks in.
    """
    start = html.index("// Slice-timeline: ordered stage names")
    end = html.index("// Slice-effort: the 4-segment picker")
    return html[start:end]


SCRIPT = r"""
// Minimal stubs: we don't need real React, just something that
// records the element tree so the test can traverse it.
globalThis.h = (type, props, ...children) => ({
  type, props: props || {}, children: children.flat().filter(c => c != null),
});
// Simple useState-alike: just hold a value we can read back. We don't
// need setter-driven re-renders for this test harness — the component
// renders once post-effect-resolution.
let _stateStore = [];
let _stateIdx = 0;
globalThis.useState = (initial) => {
  const idx = _stateIdx++;
  if (_stateStore.length <= idx) _stateStore.push(initial);
  return [_stateStore[idx], v => {
    _stateStore[idx] = typeof v === "function" ? v(_stateStore[idx]) : v;
  }];
};
// Register but never auto-invoke the cleanup — otherwise the effect's
// ``cancelled`` flag flips true before the fetch resolves, and the
// effect silently drops its setState call.
globalThis.useEffect = (fn, _deps) => { fn(); };
globalThis.useRef = (initial) => ({current: initial});

// fetch stub swapped per test via globalThis.__nextFetchBody
globalThis.fetch = async (_url) => {
  const body = globalThis.__nextFetchBody || {};
  return {
    ok: true,
    status: 200,
    json: async () => body,
  };
};

async function render(slug, body) {
  _stateStore = []; _stateIdx = 0;
  globalThis.__nextFetchBody = body;
  // First pass runs the effect (which triggers fetch → sets state).
  // Render once; then re-render after a microtask flush so the state
  // set by the effect is visible to the view.
  TimelineDots({slug, refreshKey: 0});
  // Pump the microtask queue so the awaited fetch resolves.
  await new Promise(r => setTimeout(r, 0));
  _stateIdx = 0;  // re-use stored state on the second render pass
  return TimelineDots({slug, refreshKey: 0});
}

%(source)s

function flatten(tree, acc) {
  if (!tree || typeof tree !== "object") return;
  acc.push(tree);
  for (const c of tree.children || []) flatten(c, acc);
}

const out = {};

// Fixture 1: a fully-populated timeline.
const populated = {
  slug: "feat-slice-timeline",
  stages: [
    {stage: "design", status: "success", count: 1, latest: {task_id: "t1", started_at: "2026-11-17T09:00:00-05:00", ended_at: "2026-11-17T09:10:00-05:00", summary: "design note landed"}, runs: []},
    {stage: "build", status: "running", count: 1, latest: {task_id: "t2", started_at: "2026-11-17T10:00:00-05:00", ended_at: null, summary: ""}, runs: []},
    {stage: "review", status: "failed", count: 1, latest: {task_id: "t3", started_at: "2026-11-17T11:00:00-05:00", ended_at: "2026-11-17T11:05:00-05:00", summary: "lint flagged"}, runs: []},
    {stage: "check", status: "none", count: 0, latest: null, runs: []},
    {stage: "merge", status: "none", count: 0, latest: null, runs: []},
  ],
};
const tree = await render("feat-slice-timeline", populated);
const flat = [];
flatten(tree, flat);
// The 5 dot spans — identified by className prefix "woy-stage-dot ".
const dots = flat.filter(n => (n.props.className || "").startsWith("woy-stage-dot "));
out.dotCount = dots.length;
out.statusClasses = dots.map(d => d.props.className);
out.stageOrder = dots.map(d => d.props["data-stage"]);
// Tooltip reads summary + task_id for a success dot (design).
out.designTitle = dots[0].props.title;
out.buildTitle = dots[1].props.title;
out.reviewTitle = dots[2].props.title;
out.checkTitle = dots[3].props.title;

// Fixture 2: empty slug — API returns 5 none-stages.
const empty = {
  slug: "nothing-touched",
  stages: ["design", "build", "review", "check", "merge"].map(s => ({stage: s, status: "none", count: 0, latest: null, runs: []})),
};
const tree2 = await render("nothing-touched", empty);
const flat2 = [];
flatten(tree2, flat2);
const dots2 = flat2.filter(n => (n.props.className || "").startsWith("woy-stage-dot "));
out.emptyDotCount = dots2.length;
out.emptyStatusClasses = dots2.map(d => d.props.className);
// The empty hint span appears when no stage has runs.
const emptyHint = flat2.find(n => (n.props.className || "") === "woy-stage-empty");
out.emptyHintShown = !!emptyHint;

process.stdout.write(JSON.stringify(out));
"""


def _run(source: str) -> dict:
    """Run the extracted source + probe under node and parse the printed JSON."""
    p = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT % {"source": source}],
        capture_output=True, text=True, timeout=10,
    )
    if p.returncode != 0:
        raise AssertionError(f"node failed:\nstderr:\n{p.stderr}\nstdout:\n{p.stdout}")
    return json.loads(p.stdout)


@pytest.fixture(scope="module")
def results() -> dict:
    html = (STATIC_DIR / "index.html").read_text()
    source = _extract_source(html)
    return _run(source)


def test_populated_renders_five_dots_in_stage_order(results):
    assert results["dotCount"] == 5
    assert results["stageOrder"] == ["design", "build", "review", "check", "merge"]


def test_populated_dot_status_classes_match_rollup(results):
    classes = results["statusClasses"]
    assert "status-success" in classes[0]
    assert "status-running" in classes[1]
    assert "status-failed" in classes[2]
    assert "status-none" in classes[3]
    assert "status-none" in classes[4]


def test_populated_tooltip_includes_task_id_and_summary(results):
    """Hovering a dot that has runs should show the summary + task id so
    Jason doesn't need to open any modal to know what happened."""
    assert "t1" in results["designTitle"]
    assert "design note landed" in results["designTitle"]
    assert "success" in results["designTitle"]
    # Running stage shows "running" and task_id, no ended_at.
    assert "running" in results["buildTitle"]
    assert "t2" in results["buildTitle"]
    assert "failed" in results["reviewTitle"]
    # Untouched stage says "no runs yet".
    assert "no runs yet" in results["checkTitle"]


def test_empty_renders_five_grey_dots_and_empty_hint(results):
    """An item with no history shows five grey dots + the empty hint."""
    assert results["emptyDotCount"] == 5
    assert all("status-none" in c for c in results["emptyStatusClasses"])
    assert results["emptyHintShown"] is True
