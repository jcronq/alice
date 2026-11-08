"""Tests for the status-site Resources renderer (issue #860)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


from alice_state_db.schema import apply_schema
from status_site.sources import resources as R
from status_site.sources.resources import (
    SURFACE_ID,
    SnapshotRow,
    TimelineBucket,
    TokenTotal,
    build_envelopes,
    fetch_snapshot,
    fetch_timeline,
    fetch_token_totals,
    render,
    render_resources_md,
    render_snapshot_table,
    render_timeline_svg,
    render_token_table,
)


def _seed_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    apply_schema(conn)
    return conn


def _iso(mins_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    ).isoformat(timespec="seconds")


# ─── fetch_snapshot ───────────────────────────────────────────────


def test_fetch_snapshot_returns_latest_per_pid(tmp_path):
    conn = _seed_db(tmp_path / "state.db")
    rows = [
        (_iso(1.5), None, 101, "thinking", 50.0, 400.0, "ps"),
        (_iso(0.5), None, 101, "thinking", 60.0, 410.0, "ps"),  # latest for 101
        (_iso(0.2), "bg_z", 202, "claude", 90.0, 1200.0, "ps"),
    ]
    conn.executemany(
        "INSERT INTO resource_samples (ts, bg_id, pid, cmd_tag, cpu_pct, rss_mb, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    snap = fetch_snapshot(conn)
    # Two distinct pids; row 101 picks the newest (60% CPU).
    by_pid = {r.pid: r for r in snap}
    assert by_pid[101].cpu_pct == 60.0
    assert by_pid[202].bg_id == "bg_z"


def test_fetch_snapshot_ignores_stale_samples(tmp_path):
    conn = _seed_db(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO resource_samples (ts, bg_id, pid, cmd_tag, cpu_pct, rss_mb, source) "
        "VALUES (?, NULL, 999, 'thinking', 5.0, 100.0, 'ps')",
        (_iso(60),),  # one hour old
    )
    conn.commit()
    assert fetch_snapshot(conn) == []


# ─── fetch_timeline ────────────────────────────────────────────────


def test_fetch_timeline_buckets_and_series(tmp_path):
    conn = _seed_db(tmp_path / "state.db")
    base = [
        (_iso(90), "bg_a", 1, "claude", 40.0, 100.0, "ps"),
        (_iso(85), "bg_a", 1, "claude", 60.0, 100.0, "ps"),
        (_iso(30), None, 2, "thinking", 15.0, 50.0, "ps"),
    ]
    conn.executemany(
        "INSERT INTO resource_samples (ts, bg_id, pid, cmd_tag, cpu_pct, rss_mb, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        base,
    )
    conn.commit()
    tl = fetch_timeline(conn)
    by_series = sorted({b.series for b in tl})
    assert "bg_a" in by_series
    assert "cmd_tag:thinking" in by_series


# ─── fetch_token_totals ────────────────────────────────────────────


def test_fetch_token_totals_groups_by_worker_and_model(tmp_path):
    conn = _seed_db(tmp_path / "state.db")
    rows = [
        (_iso(10), "bg_a", "/x.jsonl", 0, "claude-opus-4-7", 100, 50, 0, 0),
        (_iso(9), "bg_a", "/x.jsonl", 1, "claude-opus-4-7", 20, 10, 5, 2),
        (_iso(8), "bg_b", "/y.jsonl", 0, "claude-sonnet-4-5", 1000, 500, 0, 0),
    ]
    conn.executemany(
        "INSERT INTO token_usage (ts, bg_id, session_path, turn_index, model, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    totals = fetch_token_totals(conn)
    by_key = {(t.bg_id, t.model): t for t in totals}
    assert by_key[("bg_a", "claude-opus-4-7")].input_tokens == 120
    assert by_key[("bg_a", "claude-opus-4-7")].output_tokens == 60
    assert by_key[("bg_b", "claude-sonnet-4-5")].input_tokens == 1000


# ─── renderers ─────────────────────────────────────────────────────


def test_render_snapshot_table_has_header_and_rows():
    rows = [
        SnapshotRow(pid=1, cmd_tag="claude", bg_id="bg_1234567890abc",
                    cpu_pct=12.5, rss_mb=300.0, ts="2026-11-07T00:00:00+00:00"),
    ]
    md = render_snapshot_table(rows, title="Top by CPU")
    assert "### Top by CPU" in md
    assert "bg_12345…" in md  # truncated
    assert "12.5" in md
    assert "| PID |" in md


def test_render_snapshot_table_empty_placeholder():
    md = render_snapshot_table([], title="Top by CPU")
    assert "(no samples yet)" in md


def test_render_timeline_svg_contains_svg_when_data():
    buckets = []
    base = datetime(2026, 11, 7, tzinfo=timezone.utc)
    for i in range(3):
        buckets.append(TimelineBucket(
            bucket_start=base + timedelta(minutes=5 * i),
            series="bg_a", cpu_pct=10.0 * (i + 1),
        ))
        buckets.append(TimelineBucket(
            bucket_start=base + timedelta(minutes=5 * i),
            series="cmd_tag:thinking", cpu_pct=5.0,
        ))
    svg = render_timeline_svg(buckets)
    assert "<svg" in svg
    assert "polygon" in svg
    assert "Legend" in svg


def test_render_timeline_svg_empty_placeholder():
    out = render_timeline_svg([])
    assert "<svg" not in out
    assert "warming up" in out


def test_render_token_table_formats_numbers():
    rows = [
        TokenTotal(bg_id="bg_x", model="claude-opus-4-7",
                   input_tokens=1234567, output_tokens=200, cache_creation_tokens=0, cache_read_tokens=0),
    ]
    md = render_token_table(rows)
    assert "1,234,567" in md
    assert "claude-opus-4-7" in md


def test_render_resources_md_recovers_from_bad_db(monkeypatch):
    def _broken(db_path=None):  # noqa: ARG001
        raise RuntimeError("no db")

    monkeypatch.setattr(R, "_connect", _broken)
    out = render_resources_md()
    assert "# Resources" in out
    assert "cannot open state DB" in out


class _ConnCM:
    """Minimal context manager wrapping a live sqlite3 connection.

    Mirrors the shape of ``alice_state_db.read_api.connect``: yielding
    on ``__enter__``, closing on ``__exit__``.
    """

    def __init__(self, path: str):
        self._path = path
        self._conn = None

    def __enter__(self):
        self._conn = sqlite3.connect(self._path)
        return self._conn

    def __exit__(self, *exc):
        if self._conn is not None:
            self._conn.close()


# ─── A2UI surface (page:resources) ─────────────────────────────────


def test_build_envelopes_returns_resources_surface(monkeypatch):
    """The A2UI envelope list uses surface id ``resources`` and carries
    the markdown body on a ``text`` component so the shell's generic
    A2uiSurface renderer mounts it on the ``#resources`` route.
    """
    monkeypatch.setattr(
        R, "render_resources_md",
        lambda db_path=None, now=None: "# Resources\n\nhello body\n",
    )
    envs = build_envelopes()
    assert isinstance(envs, list) and len(envs) >= 2
    # createSurface opens the right surface id.
    create = envs[0]
    assert create["type"] == "createSurface"
    assert create["surfaceId"] == SURFACE_ID == "resources"
    # updateComponents carries a root list and a text node with the body.
    update = next(e for e in envs if e["type"] == "updateComponents")
    assert update["surfaceId"] == "resources"
    comps = {c["id"]: c for c in update["components"]}
    assert "root" in comps and comps["root"]["type"] == "list"
    # Body text carries a non-empty markdown prop.
    body = comps.get("body")
    assert body is not None, "body component missing"
    assert body["type"] == "text"
    assert body["props"]["markdown"].strip().startswith("# Resources")
    assert "hello body" in body["props"]["markdown"]


def test_render_wraps_envelopes_in_a2ui_payload(monkeypatch):
    """``render()`` wraps :func:`build_envelopes` in the ``{"a2ui": […]}``
    section-payload shape the broadcaster expects.
    """
    monkeypatch.setattr(
        R, "render_resources_md",
        lambda db_path=None, now=None: "# Resources\n\nx\n",
    )
    out = render(cfg=None)
    assert isinstance(out, dict)
    assert "a2ui" in out
    assert isinstance(out["a2ui"], list)
    # Same envelope list as build_envelopes (not a snapshot — must
    # carry the full createSurface + updateComponents pair).
    types = [e["type"] for e in out["a2ui"]]
    assert "createSurface" in types and "updateComponents" in types


def test_render_resources_md_end_to_end(tmp_path, monkeypatch):
    conn = _seed_db(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO resource_samples (ts, bg_id, pid, cmd_tag, cpu_pct, rss_mb, source) "
        "VALUES (?, 'bg_z', 10, 'claude', 42.0, 500.0, 'ps')",
        (_iso(0.1),),
    )
    conn.execute(
        "INSERT INTO token_usage (ts, bg_id, session_path, turn_index, model, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens) "
        "VALUES (?, 'bg_z', '/x.jsonl', 0, 'claude-opus-4-7', 99, 11, 0, 0)",
        (_iso(0.1),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        R, "_connect",
        lambda db_path=None: _ConnCM(str(tmp_path / "state.db")),
    )
    out = render_resources_md()
    assert "# Resources" in out
    assert "Top by CPU" in out
    assert "bg_z" in out
    assert "Token usage" in out
    # Size constraint: well under 500 KB for this small dataset.
    assert len(out.encode("utf-8")) < 50 * 1024
    # Staleness footer present.
    assert "last_rendered" in out


# ─── route wiring (static/index.html parseHash) ────────────────────


def test_parse_hash_whitelists_resources_route():
    """``parseHash`` in ``static/index.html`` must send the literal
    ``resources`` hash to the resources route — otherwise the nav tab
    silently falls through to the Overview board (the PR #861 bug).
    The check is a regex over the shipped HTML so a future refactor
    that drops the branch fails this test instead of regressing
    silently on the live site.
    """
    html = (
        Path(__file__).resolve().parents[1]
        / "src" / "status_site" / "static" / "index.html"
    ).read_text()
    assert re.search(
        r'h\s*===\s*"resources"\s*\)\s*return\s*\{\s*route:\s*"resources"',
        html,
    ), "parseHash branch for #resources hash is missing"
