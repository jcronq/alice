"""Tests for the live-rendered Alice's queue section + ``/api/surfaces``.

Covers ``scan_surfaces`` / ``render_alice_queue`` / the ``/index.md``
splice intercept / the ``GET /api/surfaces`` route. Mirrors the fixture
shape in ``test_status_site_chat.py``: tmp_path ``Config``,
``make_server`` on an ephemeral port, live ``http.client`` requests.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from status_site.server import (
    ALICE_QUEUE_END,
    ALICE_QUEUE_START,
    Config,
    _md_escape,
    _splice_sections,
    _surface_label,
    _surface_link_md,
    _surface_link_url,
    format_age,
    make_server,
    render_alice_queue,
    scan_surfaces,
    surfaces_api_payload,
)

KEY = "queue-key-xyz"


# ---------------------------------------------------------------- helpers


def write_surface(
    surface_dir: Path,
    *,
    created: datetime,
    slug: str,
    type_: str,
    page: str | None = None,
    thread: str | None = None,
    detail: str | None = None,
    source: str = "status-site",
    body: str = "SECRET BODY — must never leak into the queue render\n",
    mtime: float | None = None,
) -> Path:
    """Write ``<ISO>-<slug>.md`` with minimal frontmatter.

    ``body`` deliberately contains a sentinel string so no-leakage tests
    can prove the render/API never reach into the message text.
    """
    surface_dir.mkdir(parents=True, exist_ok=True)
    stamp = created.strftime("%Y-%m-%dT%H-%M-%S")
    lines = ["---", f"type: {type_}", f"source: {source}"]
    if page is not None:
        lines.append(f"page: {page}")
    if thread is not None:
        lines.append(f"thread: {thread}")
    if detail is not None:
        lines.append(f"detail: {detail}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    p = surface_dir / f"{stamp}-{slug}.md"
    p.write_text("\n".join(lines))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def req(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    out = r.status, r.read(), dict(r.getheaders())
    c.close()
    return out


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


# ---------------------------------------------------------------- unit: age + escape


def test_format_age_buckets():
    assert format_age(5) == "5s"
    assert format_age(59) == "59s"
    assert format_age(60) == "1m"
    assert format_age(59 * 60 + 30) == "59m"
    assert format_age(60 * 60) == "1h"
    assert format_age(3 * 3600 + 12 * 60) == "3h 12m"
    assert format_age(24 * 3600) == "1d"
    assert format_age(2 * 24 * 3600 + 5 * 3600) == "2d 5h"
    # Negative clock skew clamps to zero.
    assert format_age(-100) == "0s"


def test_md_escape_handles_metacharacters():
    assert _md_escape("plain-slug") == "plain-slug"
    assert _md_escape("has*star") == "has\\*star"
    assert _md_escape("bad[name]") == "bad\\[name\\]"
    assert _md_escape("under_score") == "under\\_score"
    assert _md_escape("`backtick`") == "\\`backtick\\`"


def test_surface_label_and_link_rules():
    chat = {"type": "status-chat", "slug": "status-chat", "detail": ""}
    assert _surface_label(chat) == "chat"
    assert _surface_link_md(chat) == "[open chat](?p=chat)"
    assert _surface_link_url(chat) == "?p=chat"

    reply = {
        "type": "status-reply",
        "slug": "status-reply-fix-the-thing",
        "detail": "status-reply-fix-the-thing",
        "page": "bugs",
    }
    assert _surface_label(reply) == "reply: fix-the-thing"
    assert _surface_link_md(reply) == "[bugs](?p=bugs)"
    assert _surface_link_url(reply) == "?p=bugs"

    gh = {
        "type": "stage-b-unclassified",
        "slug": "stage-b-unclassified-note-github-jcronq-alice-issue608-opened",
        "detail": "",
    }
    assert _surface_label(gh) == "stage-b-unclassified"
    assert (
        _surface_link_md(gh)
        == "[issue #608](https://github.com/jcronq/alice/issues/608)"
    )
    assert _surface_link_url(gh) == "https://github.com/jcronq/alice/issues/608"

    unknown = {"type": "conflict-candidate", "slug": "x", "detail": ""}
    assert _surface_label(unknown) == "conflict-candidate"
    assert _surface_link_md(unknown) == ""
    assert _surface_link_url(unknown) == ""


# ---------------------------------------------------------------- render


def test_render_empty_state():
    assert render_alice_queue([], now=100.0) == "*(queue clear)*"


def test_render_mixed_surfaces_shape(tmp_path):
    base = datetime(2026, 11, 5, 10, 0, 0)
    write_surface(
        tmp_path / "surface",
        created=base,
        slug="status-chat",
        type_="status-chat",
        page="chat",
    )
    write_surface(
        tmp_path / "surface",
        created=datetime(2026, 11, 5, 10, 15, 0),
        slug="status-reply-bug-2",
        type_="status-reply",
        page="bugs",
        thread="bug-2",
        detail="status-reply-bug-2",
    )
    write_surface(
        tmp_path / "surface",
        created=datetime(2026, 11, 5, 10, 25, 0),
        slug="stage-b-unclassified-note-github-jcronq-alice-issue608-opened",
        type_="stage-b-unclassified",
    )
    write_surface(
        tmp_path / "surface",
        created=datetime(2026, 11, 5, 10, 30, 0),
        slug="conflict-candidate-abc",
        type_="conflict-candidate",
    )
    items = scan_surfaces(tmp_path / "surface")
    # Oldest first.
    assert [it["type"] for it in items] == [
        "status-chat",
        "status-reply",
        "stage-b-unclassified",
        "conflict-candidate",
    ]
    # Pin render time well after the stamps so ages are stable.
    now = datetime(2026, 11, 5, 14, 0, 0).timestamp()
    body = render_alice_queue(items, now=now)
    assert "*What's pending my attention. Live from `inner/surface/`" in body
    # chat
    assert "- 4h — **chat** [open chat](?p=chat)" in body
    # reply with page link
    assert "- 3h 45m — **reply: bug-2** [bugs](?p=bugs)" in body
    # stage-b unclassified → issue link
    assert (
        "**stage-b-unclassified** "
        "[issue #608](https://github.com/jcronq/alice/issues/608)"
    ) in body
    # unknown type → bare label, no link
    assert body.rstrip().endswith("**conflict-candidate**")


def test_render_no_heading_included():
    """The region body excludes the ``## Alice's queue`` heading itself."""
    assert "## Alice's queue" not in render_alice_queue([], now=100.0)
    items = [
        {
            "slug": "x",
            "type": "status-chat",
            "detail": "",
            "page": "",
            "created_epoch": 100.0,
        }
    ]
    assert "## Alice's queue" not in render_alice_queue(items, now=200.0)


def test_render_slug_markdown_escaped(tmp_path):
    base = datetime(2026, 11, 5, 10, 0, 0)
    # A stage-b note whose slug carries a markdown metacharacter.
    p = write_surface(
        tmp_path / "surface",
        created=base,
        slug="wild-name",
        type_="status-reply",
        page="bugs",
        detail="status-reply-wild*name[danger]",
    )
    assert p.exists()
    items = scan_surfaces(tmp_path / "surface")
    body = render_alice_queue(items, now=base.timestamp() + 60)
    # Escaped forms land in the body.
    assert "wild\\*name\\[danger\\]" in body
    # Raw forms don't.
    assert "wild*name[danger]" not in body


# ---------------------------------------------------------------- scan: exclusions


def test_scan_skips_handled_dotfiles_and_tmp(tmp_path):
    sdir = tmp_path / "surface"
    sdir.mkdir()
    base = datetime(2026, 11, 5, 9, 0, 0)
    write_surface(sdir, created=base, slug="status-chat", type_="status-chat")
    # Handled dir — should never appear.
    handled = sdir / ".handled" / "2026-11-04"
    handled.mkdir(parents=True)
    (handled / "2026-11-04T10-00-00-old-reply.md").write_text(
        "---\ntype: status-reply\n---\n\nold\n"
    )
    # Dotfile in-flight write.
    (sdir / ".tmp-chat-12345.md").write_text("junk\n")
    # Gitkeep.
    (sdir / ".gitkeep").write_text("")
    items = scan_surfaces(sdir)
    assert [it["slug"] for it in items] == ["status-chat"]


def test_scan_missing_directory_returns_empty(tmp_path):
    assert scan_surfaces(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------- scan: robustness


def test_scan_mtime_fallback_for_bad_prefix(tmp_path):
    sdir = tmp_path / "surface"
    sdir.mkdir()
    p = sdir / "no-timestamp-prefix.md"
    p.write_text("---\ntype: oddball\n---\n\nbody\n")
    os.utime(p, (1_700_000_000, 1_700_000_000))
    items = scan_surfaces(sdir)
    assert len(items) == 1
    assert items[0]["slug"] == "no-timestamp-prefix"
    # created_epoch falls back to mtime (local-TZ, so the exact value
    # depends on the host; just compare to the file's own timestamp).
    assert items[0]["created_epoch"] == datetime.fromtimestamp(
        1_700_000_000
    ).timestamp()


def test_scan_invalid_utf8_does_not_crash(tmp_path):
    sdir = tmp_path / "surface"
    sdir.mkdir()
    base = datetime(2026, 11, 5, 9, 0, 0)
    stamp = base.strftime("%Y-%m-%dT%H-%M-%S")
    p = sdir / f"{stamp}-binary-nonsense.md"
    # Invalid UTF-8 bytes; read_text(errors='replace') must swallow it.
    p.write_bytes(b"---\ntype: binary\n---\n\n\xff\xfe\xfd not valid utf-8\n")
    items = scan_surfaces(sdir)
    assert len(items) == 1
    assert items[0]["type"] == "binary"


def test_scan_unreadable_file_is_skipped(tmp_path, monkeypatch):
    """A single bad file must not 500 the whole scan."""
    sdir = tmp_path / "surface"
    sdir.mkdir()
    base = datetime(2026, 11, 5, 9, 0, 0)
    good = write_surface(sdir, created=base, slug="good", type_="status-chat")
    bad = write_surface(
        sdir,
        created=datetime(2026, 11, 5, 9, 1, 0),
        slug="bad",
        type_="status-chat",
    )
    # Force a ``read_text`` OSError on exactly the bad file.
    real = Path.read_text

    def boom(self, *a, **kw):
        if self == bad:
            raise OSError("simulated unreadable")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    items = scan_surfaces(sdir)
    assert [it["path"] for it in items] == [good]


# ---------------------------------------------------------------- cap + overflow


def test_render_cap_ten_and_overflow_line(tmp_path):
    sdir = tmp_path / "surface"
    sdir.mkdir()
    for i in range(12):
        write_surface(
            sdir,
            created=datetime(2026, 11, 5, 9, i, 0),
            slug=f"status-chat-{i}",
            type_="status-chat",
            page="chat",
        )
    items = scan_surfaces(sdir)
    assert len(items) == 12
    now = datetime(2026, 11, 5, 10, 0, 0).timestamp()
    body = render_alice_queue(items, now=now)
    # 10 rendered item lines + 1 overflow line.
    bullet_lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == 11
    assert bullet_lines[-1] == "- +2 more — [full list](/api/surfaces)"


# ---------------------------------------------------------------- splice


def test_splice_replaces_between_markers():
    body = (
        "# Overview\n\n"
        "## Alice's queue\n"
        f"{ALICE_QUEUE_START}\n"
        "old placeholder\n"
        f"{ALICE_QUEUE_END}\n"
        "## Thinking queue\n\ntail\n"
    )
    out = _splice_sections(
        body,
        [
            (
                ALICE_QUEUE_START,
                ALICE_QUEUE_END,
                lambda: "RENDERED-REGION",
            )
        ],
    )
    # Markers survive, placeholder is gone, rendered region sits between.
    assert "old placeholder" not in out
    assert ALICE_QUEUE_START in out and ALICE_QUEUE_END in out
    assert "RENDERED-REGION" in out
    # Section order is preserved.
    assert out.index("## Alice's queue") < out.index("## Thinking queue")


def test_splice_is_idempotent():
    body = (
        "# Overview\n\n"
        f"{ALICE_QUEUE_START}\n"
        "placeholder\n"
        f"{ALICE_QUEUE_END}\n"
    )
    once = _splice_sections(
        body,
        [(ALICE_QUEUE_START, ALICE_QUEUE_END, lambda: "RENDERED")],
    )
    twice = _splice_sections(
        once,
        [(ALICE_QUEUE_START, ALICE_QUEUE_END, lambda: "RENDERED")],
    )
    assert once == twice


def test_splice_appends_when_markers_missing():
    body = "# Overview\n\n(no markers here)\n"

    def fallback(rendered: str) -> str:
        return f"\n## Alice's queue\n{ALICE_QUEUE_START}\n{rendered}\n{ALICE_QUEUE_END}\n"

    out = _splice_sections(
        body,
        [
            (
                ALICE_QUEUE_START,
                ALICE_QUEUE_END,
                lambda: "RENDERED",
                fallback,
            )
        ],
    )
    assert "(no markers here)" in out  # original body untouched
    assert out.rstrip().endswith(ALICE_QUEUE_END)
    assert "RENDERED" in out


# ---------------------------------------------------------------- HTTP routes


def test_http_index_md_empty_queue_renders_empty_state(site):
    cfg, port = site
    (cfg.status_dir / "index.md").write_text(
        "# Overview\n\n"
        "## Alice's queue\n"
        f"{ALICE_QUEUE_START}\n"
        "placeholder\n"
        f"{ALICE_QUEUE_END}\n"
    )
    status, body, headers = req(port, "GET", "/index.md")
    assert status == 200
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    text = body.decode()
    assert "*(queue clear)*" in text
    assert "placeholder" not in text


def test_http_index_md_live_renders_mixed_surfaces(site):
    cfg, port = site
    (cfg.status_dir / "index.md").write_text(
        "# Overview\n\n"
        "## Alice's queue\n"
        f"{ALICE_QUEUE_START}\n"
        "placeholder\n"
        f"{ALICE_QUEUE_END}\n"
        "## Thinking queue\n\n(tail)\n"
    )
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 0, 0),
        slug="status-chat",
        type_="status-chat",
        page="chat",
    )
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 5, 0),
        slug="status-reply-bug-2",
        type_="status-reply",
        page="bugs",
        detail="status-reply-bug-2",
    )
    _, body, _ = req(port, "GET", "/index.md")
    text = body.decode()
    assert "**chat** [open chat](?p=chat)" in text
    assert "**reply: bug-2** [bugs](?p=bugs)" in text
    # The on-disk placeholder was replaced.
    assert "placeholder" not in text
    # Section order preserved.
    assert text.index("## Alice's queue") < text.index("## Thinking queue")


def test_http_index_md_appends_when_markers_missing(site):
    cfg, port = site
    on_disk = "# Overview\n\n## Alice's queue\n\n- hand-maintained bullet\n"
    (cfg.status_dir / "index.md").write_text(on_disk)
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 0, 0),
        slug="status-chat",
        type_="status-chat",
        page="chat",
    )
    _, body, _ = req(port, "GET", "/index.md")
    text = body.decode()
    # The appended fallback section is present.
    assert ALICE_QUEUE_START in text and ALICE_QUEUE_END in text
    assert "**chat** [open chat](?p=chat)" in text
    # The file on disk is unchanged.
    assert (cfg.status_dir / "index.md").read_text() == on_disk


def test_http_index_md_last_modified_tracks_newest_surface(site):
    cfg, port = site
    (cfg.status_dir / "index.md").write_text(
        "# Overview\n\n"
        "## Alice's queue\n"
        f"{ALICE_QUEUE_START}\n"
        "placeholder\n"
        f"{ALICE_QUEUE_END}\n"
    )
    # Index file in the past, surface in the (relative) future.
    os.utime(cfg.status_dir / "index.md", (1_700_000_000, 1_700_000_000))
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 0, 0),
        slug="status-chat",
        type_="status-chat",
        page="chat",
        mtime=1_800_000_000,
    )
    _, _, headers = req(port, "GET", "/index.md")
    lm = parsedate_to_datetime(headers["Last-Modified"]).timestamp()
    assert lm == pytest.approx(1_800_000_000, abs=1)


def test_http_api_surfaces_empty(site):
    _, port = site
    status, body, headers = req(port, "GET", "/api/surfaces")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"count": 0, "surfaces": []}


def test_http_api_surfaces_full_list_no_cap(site):
    cfg, port = site
    for i in range(12):
        write_surface(
            cfg.surface_dir,
            created=datetime(2026, 11, 5, 9, i, 0),
            slug=f"status-chat-{i}",
            type_="status-chat",
            page="chat",
        )
    _, body, _ = req(port, "GET", "/api/surfaces")
    doc = json.loads(body)
    assert doc["count"] == 12
    assert len(doc["surfaces"]) == 12
    # Oldest-first ordering.
    assert doc["surfaces"][0]["slug"] == "status-chat-0"
    assert doc["surfaces"][-1]["slug"] == "status-chat-11"
    # Shape of each entry: labels + link, no body.
    for entry in doc["surfaces"]:
        assert set(entry) >= {"slug", "type", "source", "created", "age_seconds"}
        assert entry["link"] == "?p=chat"
        assert "body" not in entry
        assert "path" not in entry


def test_http_api_surfaces_link_rules(site):
    cfg, port = site
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 0, 0),
        slug="status-chat",
        type_="status-chat",
        page="chat",
    )
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 1, 0),
        slug="status-reply-foo",
        type_="status-reply",
        page="bugs",
        detail="status-reply-foo",
    )
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 2, 0),
        slug="stage-b-unclassified-github-jcronq-alice-issue608-opened",
        type_="stage-b-unclassified",
    )
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 3, 0),
        slug="conflict-candidate-x",
        type_="conflict-candidate",
    )
    _, body, _ = req(port, "GET", "/api/surfaces")
    links = {e["type"]: e.get("link") for e in json.loads(body)["surfaces"]}
    assert links["status-chat"] == "?p=chat"
    assert links["status-reply"] == "?p=bugs"
    assert links["stage-b-unclassified"] == (
        "https://github.com/jcronq/alice/issues/608"
    )
    # Unknown type: no link key.
    assert links["conflict-candidate"] is None


# ---------------------------------------------------------------- security


def test_no_leakage_of_body_or_attachment_paths(site):
    cfg, port = site
    (cfg.status_dir / "index.md").write_text(
        "# Overview\n\n"
        "## Alice's queue\n"
        f"{ALICE_QUEUE_START}\n"
        "placeholder\n"
        f"{ALICE_QUEUE_END}\n"
    )
    sentinel = "SECRET-BODY-TEXT-DO-NOT-LEAK"
    attach_sentinel = "/private/attach/path/not-served.bin"
    write_surface(
        cfg.surface_dir,
        created=datetime(2026, 11, 5, 10, 0, 0),
        slug="status-reply-bug-2",
        type_="status-reply",
        page="bugs",
        detail="status-reply-bug-2",
        body=(
            "# Jason replied on the status page\n\n"
            f"His new message:\n{sentinel}\n\n"
            f"Attachments (Read them; stored privately, not served):\n"
            f"  {attach_sentinel} (123 bytes)\n"
        ),
    )
    _, idx_body, _ = req(port, "GET", "/index.md")
    _, api_body, _ = req(port, "GET", "/api/surfaces")
    idx_text = idx_body.decode()
    api_text = api_body.decode()
    for haystack in (idx_text, api_text):
        assert sentinel not in haystack
        assert attach_sentinel not in haystack


# ---------------------------------------------------------------- api payload util


def test_surfaces_api_payload_shape():
    now = time.time()
    items = [
        {
            "slug": "a",
            "type": "status-chat",
            "source": "status-site",
            "page": "chat",
            "thread": "",
            "detail": "",
            "created": "2026-11-05T10:00:00",
            "created_epoch": now - 120,
            "path": Path("/dev/null"),
            "mtime": 0.0,
        }
    ]
    payload = surfaces_api_payload(items, now=now)
    assert payload["count"] == 1
    entry = payload["surfaces"][0]
    assert entry["slug"] == "a"
    assert entry["type"] == "status-chat"
    assert entry["source"] == "status-site"
    assert entry["created"] == "2026-11-05T10:00:00"
    assert entry["age_seconds"] == pytest.approx(120, abs=1)
    assert entry["link"] == "?p=chat"
    # No path / body / mtime leak into the API shape.
    assert "path" not in entry
    assert "mtime" not in entry
