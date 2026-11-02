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
    assert "*(none yet)*" not in out.split("## Open", 1)[1].split("## Fixed", 1)[0]


def test_no_bug_files_all_sections_empty(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    out, mtime = render_bugs_md(status)
    assert out.count("*(none yet)*") == 3
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
    assert "Click one for the full state" in out


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
    assert body.decode("utf-8").count("*(none yet)*") == 3


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
