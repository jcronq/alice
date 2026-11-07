"""Tests for the dynamic ``/bugs.md`` render in the status site server.

Jason's rule for this page: the server owns the Open / Fixed / Won't fix
buckets, built on every request from each ``bug-<N>.md`` detail file's
frontmatter. The file at ``inner/status/bugs.md``, if any, is ignored.
"""

from __future__ import annotations

import http.client
import os
import re
import threading
from pathlib import Path

import pytest

from status_site import server as S
from status_site.server import Config, make_server, render_bugs_md

KEY = "test-key-123"


# ---------------------------------------------------------------- helpers


def write_bug(
    status_dir: Path,
    report_id: int,
    *,
    status: str | None = "open",
    title: str | None = "placeholder title",
    summary: str | None = None,
    reported_at: str | None = None,
    body: str | None = None,
) -> Path:
    """Write a bug-<N>.md detail file with the given frontmatter + body."""
    lines = ["---", f"report_id: {report_id}"]
    if title is not None:
        lines.append(f"title: {title}")
    if status is not None:
        lines.append(f"status: {status}")
    if reported_at is not None:
        lines.append(f"reported_at: {reported_at}")
    if summary is not None:
        lines.append(f"summary: {summary}")
    lines.append("---")
    lines.append("")
    lines.append(body if body is not None else f"# bug-{report_id} — detail body\n")
    p = status_dir / f"bug-{report_id}.md"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------- pure render


def test_render_buckets_by_status(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="first open")
    write_bug(status, 2, status="fixed", title="fixed one")
    write_bug(status, 3, status="wontfix", title="wont fix me")
    out, _ = render_bugs_md(status)
    # Lines land under the right headings in the right order.
    open_idx = out.index("## Open")
    fixed_idx = out.index("## Fixed")
    wontfix_idx = out.index("## Won't fix")
    assert open_idx < fixed_idx < wontfix_idx
    open_section = out[open_idx:fixed_idx]
    fixed_section = out[fixed_idx:wontfix_idx]
    wontfix_section = out[wontfix_idx:]
    assert "bug-1 — first open" in open_section
    assert "bug-2 — fixed one" in fixed_section
    assert "bug-3 — wont fix me" in wontfix_section
    # Open uses an unchecked box; Fixed / Won't fix use the checked one.
    assert "- [ ] **bug-1 — first open**" in open_section
    assert "- [x] **bug-2 — fixed one**" in fixed_section
    assert "- [x] **bug-3 — wont fix me**" in wontfix_section


def test_empty_bucket_shows_placeholder(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open")
    out, _ = render_bugs_md(status)
    # Fixed and Won't fix have no entries.
    fixed_block = out.split("## Fixed", 1)[1].split("## ", 1)[0]
    wontfix_block = out.split("## Won't fix", 1)[1]
    assert "*(none yet)*" in fixed_block
    assert "*(none yet)*" in wontfix_block
    # The populated Open block itself must NOT have an empty-state placeholder.
    open_block = out.split("## Open", 1)[1].split("## ", 1)[0]
    assert "*(none yet)*" not in open_block


def test_no_bug_files_all_sections_empty(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    out, mtime = render_bugs_md(status)
    # Six sections now: Open / Waiting on review / In progress / Fixed / Won't fix / Dispatch failed.
    assert out.count("*(none yet)*") == 6
    assert mtime == 0.0


def test_missing_status_defaults_to_open(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 7, status=None, title="orphaned status")
    out, _ = render_bugs_md(status)
    assert "- [ ] **bug-7 — orphaned status**" in out
    # Not in Fixed / Won't fix sections.
    fixed_block = out.split("## Fixed", 1)[1].split("## ", 1)[0]
    assert "bug-7" not in fixed_block
    wontfix_block = out.split("## Won't fix", 1)[1]
    assert "bug-7" not in wontfix_block


def test_unknown_status_defaults_to_open(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 4, status="pending-triage", title="weird state")
    out, _ = render_bugs_md(status)
    assert "- [ ] **bug-4 — weird state**" in out


def test_newest_first_within_bucket(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="first")
    write_bug(status, 7, status="open", title="seventh")
    write_bug(status, 3, status="open", title="third")
    out, _ = render_bugs_md(status)
    open_block = out.split("## Open", 1)[1].split("## ", 1)[0]
    positions = [open_block.index(x) for x in ("bug-7", "bug-3", "bug-1")]
    assert positions == sorted(positions)


def test_mtime_is_max_over_contributing_files(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    p1 = write_bug(status, 1, status="open")
    p2 = write_bug(status, 2, status="fixed")
    os.utime(p1, (1_700_000_000, 1_700_000_000))
    os.utime(p2, (1_800_000_000, 1_800_000_000))
    _, mtime = render_bugs_md(status)
    assert mtime == 1_800_000_000.0


def test_title_falls_back_to_heading(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    (status / "bug-5.md").write_text(
        "---\nreport_id: 5\nstatus: open\n---\n\n"
        "# bug-5 — heading-sourced title\n\nMore body.\n"
    )
    out, _ = render_bugs_md(status)
    # The ``bug-5 —`` prefix in the heading is stripped so it isn't doubled.
    assert "- [ ] **bug-5 — heading-sourced title**" in out
    assert "bug-5 — bug-5" not in out


def test_summary_falls_back_to_reported_at(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status,
        6,
        status="open",
        title="iso summary",
        reported_at="2026-11-02T17:12:05+00:00",
    )
    out, _ = render_bugs_md(status)
    assert "*reported 2026-11-02 17:12 UTC*" in out


def test_summary_explicit_overrides_reported_at(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status,
        9,
        status="open",
        title="t",
        summary="handwritten summary",
        reported_at="2026-11-02T17:12:05+00:00",
    )
    out, _ = render_bugs_md(status)
    assert "*handwritten summary*" in out
    assert "reported 2026-11-02" not in out


def test_renders_header_and_intro_paragraph(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="t")
    out, _ = render_bugs_md(status)
    assert out.startswith("# In-game bug reports\n")
    assert "Click one for the full state" not in out


def test_ignores_non_bug_files(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    (status / "bugs.md").write_text("# junk hand-written\n")
    (status / "bug-garbage.md").write_text("---\nstatus: open\n---\nx\n")
    (status / "index.md").write_text("# idx\n")
    write_bug(status, 2, status="open", title="real one")
    out, _ = render_bugs_md(status)
    assert "junk hand-written" not in out
    assert "bug-2 — real one" in out


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "pages.json").write_text('[{"slug":"index","title":"Overview"}]')
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        port=0,
        host="127.0.0.1",
    )
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield cfg, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = r.status, r.read(), dict(r.getheaders())
    c.close()
    return out


def test_http_bugs_md_served_dynamically(site):
    cfg, port = site
    write_bug(cfg.status_dir, 1, status="wontfix", title="wont")
    write_bug(cfg.status_dir, 2, status="open", title="open two")
    status, body, headers = _get(port, "/bugs.md")
    assert status == 200
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert "Last-Modified" in headers
    text = body.decode("utf-8")
    assert "- [ ] **bug-2 — open two**" in text
    assert "- [x] **bug-1 — wont**" in text


def test_http_bugs_md_ignores_disk_stub(site):
    """A file on disk at bugs.md must not shadow the dynamic render."""
    cfg, port = site
    (cfg.status_dir / "bugs.md").write_text("# junk from disk\n")
    write_bug(cfg.status_dir, 1, status="open", title="live render")
    _, body, _ = _get(port, "/bugs.md")
    text = body.decode("utf-8")
    assert "junk from disk" not in text
    assert "bug-1 — live render" in text


def test_http_bugs_md_empty_state(site):
    cfg, port = site
    status, body, _ = _get(port, "/bugs.md")
    assert status == 200
    assert body.decode("utf-8").count("*(none yet)*") == 6


def test_http_head_bugs_md(site):
    cfg, port = site
    write_bug(cfg.status_dir, 1, status="open", title="t")
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("HEAD", "/bugs.md")
    r = c.getresponse()
    assert r.status == 200
    assert r.read() == b""
    assert r.getheader("Content-Type") == "text/markdown; charset=utf-8"
    c.close()


# ---------------------------------------------------------------- new buckets


def test_render_in_progress_bucket_between_open_and_fixed(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="o")
    write_bug(status, 2, status="in-progress", title="i")
    write_bug(status, 3, status="fixed", title="f")
    out, _ = render_bugs_md(status)
    open_idx = out.index("## Open")
    ip_idx = out.index("## In progress")
    fixed_idx = out.index("## Fixed")
    assert open_idx < ip_idx < fixed_idx
    ip_section = out[ip_idx:fixed_idx]
    assert "- [ ] **bug-2 — i**" in ip_section


def test_in_progress_bucket_renders_pr_url_link(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status,
        2,
        status="in-progress",
        title="give-cakes",
        summary="worker working",
        body="---\n"
        "report_id: 2\n"
        'title: "give-cakes"\n'
        "status: in-progress\n"
        'summary: "worker working"\n'
        "pr_url: https://github.com/jcronq/modern-mud/pull/42\n"
        "---\n\n# bug-2 — give-cakes\n",
    )
    # Overwrite with an explicit frontmatter so the pr_url actually appears.
    (status / "bug-2.md").write_text(
        "---\n"
        "report_id: 2\n"
        'title: "give-cakes"\n'
        "status: in-progress\n"
        'summary: "worker working"\n'
        "pr_url: https://github.com/jcronq/modern-mud/pull/42\n"
        "---\n\n# bug-2 — give-cakes\n"
    )
    out, _ = render_bugs_md(status)
    # Bugs with status=in-progress AND a pr_url now get promoted to the
    # dedicated Waiting on review section so Jason can spot review-ready
    # work without scanning the still-cooking workers.
    wor_section = out.split("## Waiting on review", 1)[1].split("## In progress", 1)[0]
    assert "[draft PR](https://github.com/jcronq/modern-mud/pull/42)" in wor_section
    assert "worker working" in wor_section
    # And the item does NOT also appear under In progress.
    ip_section = out.split("## In progress", 1)[1].split("## Fixed", 1)[0]
    assert "bug-2" not in ip_section


def test_in_progress_without_pr_url_renders_plain_summary(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 2, status="in-progress", title="t", summary="working on it")
    out, _ = render_bugs_md(status)
    ip_section = out.split("## In progress", 1)[1].split("## Fixed", 1)[0]
    assert "working on it" in ip_section
    assert "[draft PR]" not in ip_section


def test_dispatch_failed_bucket_after_wontfix_with_warning_prefix(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 7, status="dispatch-failed", title="stalled")
    out, _ = render_bugs_md(status)
    wontfix_idx = out.index("## Won't fix")
    df_idx = out.index("## Dispatch failed")
    assert df_idx > wontfix_idx
    df_section = out[df_idx:]
    # Warning glyph is prepended so Jason spots the triage items.
    assert "⚠ bug-7" in df_section
    assert "stalled" in df_section
    assert "needs triage" in out[df_idx : df_idx + 80]


def test_dispatch_failed_bucket_empty_placeholder(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="t")
    out, _ = render_bugs_md(status)
    df_section = out.split("## Dispatch failed", 1)[1]
    assert "*(none yet)*" in df_section


def test_render_all_six_sections_present_even_when_empty(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    out, _ = render_bugs_md(status)
    for heading in (
        "## Open",
        "## Waiting on review",
        "## In progress",
        "## Fixed",
        "## Won't fix",
        "## Dispatch failed (needs triage)",
    ):
        assert heading in out
    assert out.count("*(none yet)*") == 6


def test_parse_frontmatter_no_block():
    fm, body = S.parse_frontmatter("# no front\n\nbody\n")
    assert fm == {}
    assert body == "# no front\n\nbody\n"


def test_parse_frontmatter_handles_quoted_values():
    fm, body = S.parse_frontmatter(
        '---\ntitle: "quoted value"\n'
        "report_id: 42\nstatus: open\n---\n\nhello\n"
    )
    assert fm["title"] == "quoted value"
    assert fm["report_id"] == "42"
    assert fm["status"] == "open"
    assert body.startswith("\nhello")


# ---------------------------------------------------------------- A2UI emitter (#651)


def test_bugs_a2ui_envelope_shape(tmp_path):
    """``sources.bugs.render`` emits createSurface → updateComponents →
    two updateDataModel envelopes (``/sections`` + ``/pipeline``).
    """
    from status_site.sources import bugs as bugs_page

    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="open one")
    write_bug(status, 2, status="fixed", title="closed one")

    payload = bugs_page.render(status)
    assert "a2ui" in payload
    envs = payload["a2ui"]
    assert [e["type"] for e in envs] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
        "updateDataModel",
    ]
    assert all(e["surfaceId"] == "bugs" for e in envs)

    # Sections data model carries one bucket per heading, in canonical
    # order (Open first, Dispatch-failed last).
    sections_env = next(
        e for e in envs if e["type"] == "updateDataModel" and e["path"] == "/sections"
    )
    headings = [s["heading"] for s in sections_env["value"]]
    assert headings == [
        "Open",
        "Waiting on review",
        "In progress",
        "Fixed",
        "Won't fix",
        "Dispatch failed (needs triage)",
    ]
    open_rows = next(s for s in sections_env["value"] if s["key"] == "open")["rows"]
    assert len(open_rows) == 1
    assert open_rows[0]["report_id"] == 1
    assert open_rows[0]["title"] == "bug-1 — open one"


def test_bugs_a2ui_dispatch_failed_prefix(tmp_path):
    """Dispatch-failed bucket rows carry the ⚠ warning prefix so the
    client doesn't have to re-derive it.
    """
    from status_site.sources import bugs as bugs_page

    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 9, status="dispatch-failed", title="stalled one")
    payload = bugs_page.render(status)
    sections = next(
        e for e in payload["a2ui"]
        if e["type"] == "updateDataModel" and e["path"] == "/sections"
    )["value"]
    df = next(s for s in sections if s["key"] == "dispatch-failed")
    assert df["rows"][0]["title"].startswith("⚠ bug-9 —")


def test_bugs_a2ui_pipeline_null_until_priced(tmp_path):
    """No priced bugs → ``/pipeline`` data-model entry is ``None`` so the
    client hides the P&L pill (parity with ``render_bugs_md``'s "omit
    the segment entirely").
    """
    from status_site.sources import bugs as bugs_page

    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", title="unpriced")
    payload = bugs_page.render(status)
    pipeline = next(
        e for e in payload["a2ui"]
        if e["type"] == "updateDataModel" and e["path"] == "/pipeline"
    )
    assert pipeline["value"] is None


def test_bugs_a2ui_pipeline_populated_once_priced(tmp_path):
    from status_site.sources import bugs as bugs_page

    status = tmp_path / "status"
    status.mkdir()
    (status / "bug-1.md").write_text(
        "---\nreport_id: 1\ntitle: priced\nstatus: fixed\n"
        "estimate_usd: 0.25\nactual_usd: 0.40\n---\n# bug-1\n"
    )
    payload = bugs_page.render(status)
    pipeline = next(
        e for e in payload["a2ui"]
        if e["type"] == "updateDataModel" and e["path"] == "/pipeline"
    )
    assert pipeline["value"] == {
        "estimate": "$0.25",
        "actual": "$0.40",
        "delta": "$0.15",
    }
