"""``POST /api/bug-action`` — inline Approve/Deny on the Bugs page.

Covers the server endpoint that backs the Approve/Deny buttons rendered
on each ``waiting-on-review`` bug tile (the Chat button is pure
client-side — it navigates to the Chat tab with the composer pre-seeded
— so there's nothing to test on the server for it).

Mirrors ``test_status_site_review.py`` in shape: shared-key auth, drops
a ``type: status-bug-<action>`` surface, leaves the bug file alone (the
tile stays ``waiting-on-review`` until Speaking merges or bounces the
PR and rewrites the frontmatter).
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from status_site.server import Config, make_server

KEY = "bug-key-abc"


def write_bug(
    status_dir,
    report_id,
    *,
    status="in-progress",
    title="give-cakes",
    summary="worker working",
    pr_url="https://github.com/jcronq/modern-mud/pull/42",
):
    """Write a bug-<N>.md with a draft PR so it lands in waiting-on-review."""
    fm_lines = [
        "---",
        f"report_id: {report_id}",
        f'title: "{title}"',
        f"status: {status}",
        f'summary: "{summary}"',
    ]
    if pr_url:
        fm_lines.append(f"pr_url: {pr_url}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(f"# bug-{report_id} — {title}")
    fm_lines.append("")
    (status_dir / f"bug-{report_id}.md").write_text("\n".join(fm_lines) + "\n")


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
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def req(port, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    c.request(method, path, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    c.close()
    return out


def act(port, **kw):
    return req(port, "POST", "/api/bug-action", kw)


def surfaces(cfg):
    return sorted(cfg.surface_dir.glob("*.md")) if cfg.surface_dir.exists() else []


# ---------------------------------------------------------------- happy path


def test_approve_drops_bug_approve_surface(site):
    cfg, port = site
    write_bug(cfg.status_dir, 42)
    status, body = act(port, key=KEY, report_id=42, action="approve")
    assert status == 200 and body["ok"] and body["action"] == "approve"
    assert body["report_id"] == 42
    (f,) = surfaces(cfg)
    assert f.name.endswith("-status-bug-approve-42.md")
    doc = f.read_text()
    assert "type: status-bug-approve" in doc
    assert "action: approve" in doc
    assert "report_id: 42" in doc
    assert "pr_url: https://github.com/jcronq/modern-mud/pull/42" in doc
    assert "APPROVED" in doc
    # Footer tells Speaking what to execute on GitHub.
    assert "merge" in doc.lower()


def test_deny_drops_bug_reject_surface_with_notes(site):
    cfg, port = site
    write_bug(cfg.status_dir, 7)
    status, body = act(
        port, key=KEY, report_id=7, action="reject", notes="lint fails on tests/"
    )
    assert status == 200 and body["action"] == "reject"
    (f,) = surfaces(cfg)
    assert f.name.endswith("-status-bug-reject-7.md")
    doc = f.read_text()
    assert "type: status-bug-reject" in doc
    assert "action: reject" in doc
    assert "lint fails on tests/" in doc
    # Body title uses REJECT (the SM verb); the footer explains in
    # English-prose with "DENIED" so Speaking reads it as rejection.
    assert "REJECT" in doc
    assert "close the draft pr" in doc.lower()


def test_approve_without_notes_is_fine(site):
    cfg, port = site
    write_bug(cfg.status_dir, 1)
    assert act(port, key=KEY, report_id=1, action="approve")[0] == 200
    (f,) = surfaces(cfg)
    assert "(no notes)" in f.read_text()


def test_deny_without_notes_still_succeeds(site):
    # The server doesn't force notes on reject — the client prompts
    # for them before even POSTing. Keep the server permissive so a
    # scripted retry (or a different frontend) isn't blocked.
    cfg, port = site
    write_bug(cfg.status_dir, 2)
    assert act(port, key=KEY, report_id=2, action="reject")[0] == 200
    (f,) = surfaces(cfg)
    assert "(no notes)" in f.read_text()


# ---------------------------------------------------------------- auth + errors


def test_bad_key_is_403_and_drops_nothing(site):
    cfg, port = site
    write_bug(cfg.status_dir, 1)
    assert act(port, report_id=1, action="approve")[0] == 403
    assert act(port, key="wrong", report_id=1, action="approve")[0] == 403
    assert act(port, key=KEY[:-1], report_id=1, action="approve")[0] == 403
    assert surfaces(cfg) == []


def test_bad_action_is_400(site):
    cfg, port = site
    write_bug(cfg.status_dir, 1)
    assert act(port, key=KEY, report_id=1, action="modify")[0] == 400
    assert act(port, key=KEY, report_id=1, action="")[0] == 400
    assert act(port, key=KEY, report_id=1, action="ship")[0] == 400
    assert surfaces(cfg) == []


def test_bad_report_id_is_400(site):
    cfg, port = site
    assert act(port, key=KEY, report_id="not-a-number", action="approve")[0] == 400
    assert act(port, key=KEY, action="approve")[0] == 400  # missing
    assert surfaces(cfg) == []


def test_unknown_bug_is_404(site):
    cfg, port = site
    assert act(port, key=KEY, report_id=999, action="approve")[0] == 404
    assert surfaces(cfg) == []


def test_bug_without_pr_url_is_400(site):
    """Only waiting-on-review bugs are actionable. A bug with no pr_url
    is still in-progress (or open / fixed / …) — the tile shouldn't
    have carried an action strip, so a stale client reaching here is
    an error, not a success.
    """
    cfg, port = site
    write_bug(cfg.status_dir, 5, pr_url=None)
    status, body = act(port, key=KEY, report_id=5, action="approve")
    assert status == 400
    assert surfaces(cfg) == []


# ---------------------------------------------------------------- idempotence


def test_second_action_appends_second_surface(site):
    """Two clicks → two surface files (same timestamp → ``-2`` suffix).

    The server never erases a previous decision; Speaking is the one
    that reconciles when the bug file's frontmatter changes.
    """
    cfg, port = site
    write_bug(cfg.status_dir, 10)
    assert act(port, key=KEY, report_id=10, action="approve")[0] == 200
    assert act(port, key=KEY, report_id=10, action="approve")[0] == 200
    files = surfaces(cfg)
    assert len(files) == 2
    assert files[0].name.endswith("-status-bug-approve-10.md") or files[0].name.endswith(
        "-status-bug-approve-10-2.md"
    )


# ---------------------------------------------------------------- source emitter


def test_waiting_on_review_rows_carry_action_descriptors(tmp_path):
    """``sources.bugs._row_actions`` surfaces three action tiles on
    waiting-on-review rows and nothing on every other bucket.
    """
    from status_site.sources import bugs as bugs_page

    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="open", pr_url=None)  # open → no actions
    write_bug(status, 2)  # in-progress + pr_url → waiting-on-review → actions
    write_bug(status, 3, status="fixed", pr_url=None)  # fixed → no actions

    payload = bugs_page.render(status)
    sections = next(
        e for e in payload["a2ui"]
        if e["type"] == "updateDataModel" and e["path"] == "/sections"
    )["value"]
    by_key = {s["key"]: s for s in sections}

    # Only the waiting-on-review row carries actions.
    wor_row = by_key["waiting-on-review"]["rows"][0]
    assert wor_row["report_id"] == 2
    kinds = [a["action"] for a in wor_row["actions"]]
    assert kinds == ["bug.approve", "bug.deny", "bug.chat"]
    # Each action descriptor carries the report_id so the client can
    # POST without hoisting it onto surface-level state.
    assert all(a["report_id"] == 2 for a in wor_row["actions"])
    # Labels are the user-visible strings — matching the Review page's
    # (Approve, Modify, Reject) trio in cadence.
    assert [a["label"] for a in wor_row["actions"]] == ["Approve", "Deny", "Chat"]

    # Every other bucket's rows have empty ``actions``.
    for key in ("open", "in-progress", "fixed", "wontfix", "dispatch-failed"):
        for row in by_key[key]["rows"]:
            assert row["actions"] == [], f"{key} row should not carry actions"
