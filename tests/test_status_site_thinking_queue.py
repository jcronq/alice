"""Tests for the live Thinking queue on ``/index.md`` and its GitHub poller."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone

from alice_forge import thinking_queue_poller as P
from status_site.server import (
    THINKING_QUEUE_CAP,
    THINKING_QUEUE_END,
    THINKING_QUEUE_START,
    Config,
    make_server,
    read_thinking_queue,
    render_index_md,
    render_thinking_queue,
    splice_thinking_queue,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc).timestamp()


def issue(num, label, updated="2026-11-05T10:00:00Z", title=None):
    return {
        "number": num,
        "title": title or f"issue {num}",
        "labels": [{"name": label}],
        "updatedAt": updated,
    }


def cache(items, synced_at=NOW):
    return {"synced_at": synced_at, "items": items}


def write_cache(status, items, synced_at=NOW):
    (status / ".thinking-queue.json").write_text(json.dumps(cache(items, synced_at)))


# ------------------------------------------------------------------ render


def test_groups_and_orders_by_phase_with_age():
    out = render_thinking_queue(
        cache(
            [
                issue(5, "sm:blocked", updated="2026-11-03T12:00:00Z"),
                issue(2, "sm:designed", updated="2026-11-05T11:30:00Z"),
                issue(9, "sm:designing", updated="2026-11-05T10:00:00Z"),
                issue(3, "sm:designing"),
            ]
        ),
        now=NOW,
    )
    assert out.startswith("## Thinking queue")
    assert out.index("**Designing**") < out.index("**Designed**") < out.index("**Blocked**")
    assert out.index("#3](") < out.index("#9](")
    assert "[#9](https://github.com/jcronq/alice/issues/9) issue 9 — designing · 2h" in out
    assert "issue 2 — designed · 30m" in out
    assert "issue 5 — blocked · 2d" in out
    assert "Needs study" not in out  # empty buckets omitted
    assert "more…" not in out


def test_caps_at_15_with_more_link():
    items = [issue(n, "sm:designing") for n in range(1, 21)]
    out = render_thinking_queue(cache(items), now=NOW)
    assert out.count("- [#") == THINKING_QUEUE_CAP == 15
    assert "[5 more…](https://github.com/jcronq/alice/issues?q=" in out


def test_ignores_issues_without_a_phase_label():
    out = render_thinking_queue(cache([issue(1, "bug")]), now=NOW)
    assert "Nothing in the thinking queue" in out


def test_missing_cache_is_placeholder():
    out = render_thinking_queue(None, now=NOW)
    assert "Queue unavailable" in out


def test_stale_cache_shows_last_synced():
    out = render_thinking_queue(cache([issue(1, "sm:designing")], NOW - 3600), now=NOW)
    assert "Queue unavailable — last synced 2026-11-05 11:00 UTC" in out
    assert "#1" not in out


def test_read_missing_and_corrupt(tmp_path):
    assert read_thinking_queue(tmp_path) is None
    (tmp_path / ".thinking-queue.json").write_text("{not json")
    assert read_thinking_queue(tmp_path) is None
    (tmp_path / ".thinking-queue.json").write_text('{"synced_at": 1, "items": "x"}')
    assert read_thinking_queue(tmp_path) is None


# ------------------------------------------------------------------ splice


def test_splice_replaces_only_between_markers():
    text = f"before\n\n{THINKING_QUEUE_START}\nOLD\n{THINKING_QUEUE_END}\n\nafter\n"
    out = splice_thinking_queue(text, "## Thinking queue\n\nNEW\n")
    assert out.startswith("before\n\n")
    assert out.endswith("\n\nafter\n")
    assert "OLD" not in out and "NEW" in out
    assert out.count(THINKING_QUEUE_START) == 1


def test_splice_without_markers_appends_once():
    out = splice_thinking_queue("# Title\n\ntext\n", "BLOCK\n")
    assert out.startswith("# Title\n\ntext\n")
    assert out.count("BLOCK") == 1 and out.count(THINKING_QUEUE_START) == 1
    # Re-splicing the output is idempotent.
    assert splice_thinking_queue(out, "BLOCK2\n").count(THINKING_QUEUE_START) == 1


def test_render_index_md_mtime_is_max_of_file_and_cache(tmp_path):
    import os

    idx = tmp_path / "index.md"
    idx.write_text("# hi\n")
    write_cache(tmp_path, [issue(1, "sm:designing")], synced_at=NOW)
    os.utime(idx, (1000, 1000))
    os.utime(tmp_path / ".thinking-queue.json", (2000, 2000))
    body, mtime = render_index_md(tmp_path, now=NOW)
    assert mtime == 2000
    assert "# hi" in body and "issue 1" in body


def test_render_index_md_missing_file(tmp_path):
    assert render_index_md(tmp_path) is None


# ------------------------------------------------------------------ http


def test_http_index_md_is_spliced(tmp_path, monkeypatch):
    # Point ALICE_MIND at an empty scratch dir so the live-scan sources
    # (tracked_tasks / alice_queue) don't leak real task content (any
    # of which may contain "stale") into the spliced output.
    monkeypatch.setenv("ALICE_MIND", str(tmp_path / "nomind"))
    status = tmp_path / "status"
    status.mkdir()
    (tmp_path / "key").write_text("k")
    (status / "index.md").write_text(
        f"intro\n{THINKING_QUEUE_START}\nstale\n{THINKING_QUEUE_END}\noutro\n"
    )
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "up",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("GET", "/index.md?k=k")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200
        assert "stale" not in body
        assert "Queue unavailable" in body
        # Overview-section splicing (PR#638) may append known-issues /
        # running-now / done-today section markers after ``outro\n`` when
        # the source lacks those markers. The thinking-queue splice still
        # replaces the ``stale`` region between its own markers.
        assert body.startswith("intro\n") and "outro\n" in body
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ poller


def test_poller_dedupes_multi_label_issues():
    both = {"number": 7, "title": "t", "labels": [{"name": "sm:designing"}, {"name": "sm:blocked"}], "updatedAt": "x"}
    fetch = lambda label: [both] if label in ("sm:designing", "sm:blocked") else []
    items = P.collect(fetch)
    assert [i["number"] for i in items] == [7]
    assert items[0]["labels"] == ["sm:designing", "sm:blocked"]


def test_poller_write_is_atomic_and_readable_by_server(tmp_path):
    P.write_cache(tmp_path, [issue(1, "sm:designing")], now=NOW)
    assert [p.name for p in tmp_path.iterdir()] == [".thinking-queue.json"]
    assert read_thinking_queue(tmp_path)["items"][0]["number"] == 1
