"""Unit tests for the Overview reducer in ``static/index.html`` (#858).

The reducer + ``sectionIsMeaningful`` helper are not ES-module exports;
they live inline in the shell. We slice them out between the stable
``// ── 4. reducer`` / ``// ── 5. event-stream hook`` section-comment
markers, write the extracted source plus a tiny driver to a temp .mjs,
run it under node, and assert on the JSON the driver prints.

Skipped when node is unavailable — matches the pattern used by
``tests/test_status_site_chat_ux_js.py`` and
``tests/test_status_site_chat_actions.py``.
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

# Stable anchors — these section-comment markers have been in the file
# since the shell was slice-2'd and are visible in index.html's section
# catalogue. If they ever move, this slice will raise at import time.
START = "// ── 4. reducer"
END = "// ── 5. event-stream hook"


def _extract_reducer_source() -> str:
    html = (STATIC_DIR / "index.html").read_text()
    try:
        a = html.index(START)
        b = html.index(END, a)
    except ValueError as exc:  # pragma: no cover — guards against drift
        raise AssertionError(
            "reducer section-comment markers moved; update START/END"
        ) from exc
    return html[a:b]


DRIVER = r"""
%(reducer)s

const INITIAL_STATE = {
  sections: {}, ts: {},
  connected: false, error: null,
};

const out = {};

// ── (a) snapshot with empty in_flight after prior meaningful ────
{
  const prior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {in_flight: {pipeline: [{number: 1}]}},
    ts: 1000,
  });
  const next = reducer(prior, {
    type: "snapshot",
    sections: {in_flight: {pipeline: []}},
    ts: 2000,
  });
  out.a = {
    in_flight: next.sections.in_flight,
    ts_in_flight: next.ts.in_flight,
    connected: next.connected,
    error: next.error,
  };
}

// ── (b) first-ever snapshot with empty in_flight ────────────────
{
  const next = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {in_flight: {pipeline: []}},
    ts: 500,
  });
  out.b = {
    in_flight: next.sections.in_flight,
    ts_in_flight: next.ts.in_flight,
  };
}

// ── (c) snapshot with new non-empty in_flight replaces + bumps ts
{
  const prior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {in_flight: {pipeline: [{number: 1}]}},
    ts: 1000,
  });
  const next = reducer(prior, {
    type: "snapshot",
    sections: {in_flight: {pipeline: [{number: 2}, {number: 3}]}},
    ts: 3000,
  });
  out.c = {
    numbers: next.sections.in_flight.pipeline.map(x => x.number),
    ts_in_flight: next.ts.in_flight,
  };
}

// ── (d) markdown-only section always updates ────────────────────
{
  const prior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {known_issues: {markdown: "first body"}},
    ts: 1000,
  });
  const next = reducer(prior, {
    type: "snapshot",
    sections: {known_issues: {markdown: "second body"}},
    ts: 2000,
  });
  const nextEmpty = reducer(next, {
    type: "snapshot",
    sections: {known_issues: {markdown: ""}},
    ts: 3000,
  });
  out.d = {
    after_update: next.sections.known_issues.markdown,
    after_empty: nextEmpty.sections.known_issues.markdown,
    ts_after_empty: nextEmpty.ts.known_issues,
  };
}

// ── (e) section.update empty after meaningful prior → dropped ──
{
  const prior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {in_flight: {pipeline: [{number: 1}]}},
    ts: 1000,
  });
  const next = reducer(prior, {
    type: "section.update",
    name: "in_flight",
    data: {pipeline: []},
    ts: 2000,
  });
  out.e = {
    in_flight: next.sections.in_flight,
    ts_in_flight: next.ts.in_flight,
    sameRef: next === prior,
  };
}

// ── (f) section.update empty with no / empty prior → applied ───
{
  const applied1 = reducer(INITIAL_STATE, {
    type: "section.update",
    name: "in_flight",
    data: {pipeline: []},
    ts: 2000,
  });
  // With empty prior (e.g. first snapshot was empty).
  const emptyPrior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {in_flight: {pipeline: []}},
    ts: 1000,
  });
  const applied2 = reducer(emptyPrior, {
    type: "section.update",
    name: "in_flight",
    data: {pipeline: []},
    ts: 3000,
  });
  out.f = {
    no_prior_ts: applied1.ts.in_flight,
    no_prior_has_key: "in_flight" in applied1.sections,
    empty_prior_ts: applied2.ts.in_flight,
  };
}

// ── (g) snapshot with mixed meaningful/empty is merged per-key ─
{
  const prior = reducer(INITIAL_STATE, {
    type: "snapshot",
    sections: {
      in_flight: {pipeline: [{number: 1}]},
      alice_queue: {rows: [{slug: "x"}]},
      system_health: {markdown: "ok"},
    },
    ts: 1000,
  });
  const next = reducer(prior, {
    type: "snapshot",
    sections: {
      in_flight: {pipeline: []},                     // empty → keep prior
      alice_queue: {rows: [{slug: "y"}]},            // meaningful → replace
      system_health: {markdown: "degraded"},          // markdown → replace
    },
    ts: 2000,
  });
  out.g = {
    in_flight: next.sections.in_flight,
    alice_queue_slug: next.sections.alice_queue.rows[0].slug,
    system_health: next.sections.system_health.markdown,
    ts_in_flight: next.ts.in_flight,
    ts_alice_queue: next.ts.alice_queue,
    ts_system_health: next.ts.system_health,
  };
}

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    reducer_src = _extract_reducer_source()
    script = tmp_path_factory.mktemp("node") / "t.mjs"
    script.write_text(DRIVER % {"reducer": reducer_src})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# (a) empty snapshot after meaningful prior: keep prior, keep old ts,
# still flip connected / clear error.
def test_snapshot_empty_section_after_meaningful_is_dropped(result):
    a = result["a"]
    assert a["in_flight"] == {"pipeline": [{"number": 1}]}
    assert a["ts_in_flight"] == 1000  # NOT bumped to 2000
    assert a["connected"] is True
    assert a["error"] is None


# (b) first-ever snapshot with empty → wins (nothing to protect).
def test_first_ever_snapshot_empty_section_wins(result):
    b = result["b"]
    assert b["in_flight"] == {"pipeline": []}
    assert b["ts_in_flight"] == 500


# (c) non-empty replacement updates payload + ts.
def test_snapshot_nonempty_replaces_and_bumps_ts(result):
    c = result["c"]
    assert c["numbers"] == [2, 3]
    assert c["ts_in_flight"] == 3000


# (d) markdown-only section updates on every snapshot regardless of
# whether the next payload is "meaningful" or not.
def test_markdown_only_section_keeps_updating(result):
    d = result["d"]
    assert d["after_update"] == "second body"
    assert d["after_empty"] == ""
    assert d["ts_after_empty"] == 3000


# (e) section.update with empty data after meaningful prior → dropped.
def test_section_update_empty_after_meaningful_dropped(result):
    e = result["e"]
    assert e["in_flight"] == {"pipeline": [{"number": 1}]}
    assert e["ts_in_flight"] == 1000
    assert e["sameRef"] is True  # literal return state


# (f) section.update empty with no / empty prior → applied.
def test_section_update_empty_with_no_or_empty_prior_applied(result):
    f = result["f"]
    assert f["no_prior_has_key"] is True
    assert f["no_prior_ts"] == 2000
    assert f["empty_prior_ts"] == 3000


# (g) merged per-key: meaningful keys update, empty-vs-meaningful ones hold.
def test_snapshot_merges_per_key_not_all_or_nothing(result):
    g = result["g"]
    assert g["in_flight"] == {"pipeline": [{"number": 1}]}  # held
    assert g["alice_queue_slug"] == "y"                      # replaced
    assert g["system_health"] == "degraded"                  # replaced
    assert g["ts_in_flight"] == 1000  # NOT bumped
    assert g["ts_alice_queue"] == 2000
    assert g["ts_system_health"] == 2000
