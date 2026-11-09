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
from datetime import datetime

import pytest

from status_site.server import Config, collect_bugs, make_server, parse_frontmatter

KEY = "bug-key-abc"


def write_bug(
    status_dir,
    report_id,
    *,
    status="in-progress",
    title="give-cakes",
    summary="worker working",
    pr_url="https://github.com/jcronq/modern-mud/pull/42",
    approved_at=None,
):
    """Write a bug-<N>.md with a draft PR so it lands in waiting-on-review.

    ``approved_at`` (optional ISO-8601 string) stamps the frontmatter
    so ``collect_bugs`` stops promoting the tile back out of
    ``in-progress`` — Jason has already clicked Approve.
    """
    fm_lines = [
        "---",
        f"report_id: {report_id}",
        f'title: "{title}"',
        f"status: {status}",
        f'summary: "{summary}"',
    ]
    if pr_url:
        fm_lines.append(f"pr_url: {pr_url}")
    if approved_at:
        fm_lines.append(f"approved_at: {approved_at}")
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

    # Every other bucket's rows (that aren't in-progress) have empty
    # ``actions``. The in-progress bucket now gets a Chat-only strip —
    # its specific shape is asserted by
    # ``test_in_progress_rows_carry_chat_only`` below.
    for key in ("open", "fixed", "wontfix", "dispatch-failed"):
        for row in by_key[key]["rows"]:
            assert row["actions"] == [], f"{key} row should not carry actions"


# ---------------------------------------------------------------- approved_at round-trip


def test_approve_writes_approved_at_to_frontmatter(site):
    """Clicking Approve stamps ``approved_at`` into the bug file so
    ``collect_bugs`` stops promoting the tile back to waiting-on-review.

    The timestamp is ISO-8601 UTC and round-trips cleanly through
    ``parse_frontmatter`` — all other frontmatter fields and the body
    are preserved verbatim.
    """
    cfg, port = site
    write_bug(cfg.status_dir, 42)
    before = (cfg.status_dir / "bug-42.md").read_text()
    fm_before, body_before = parse_frontmatter(before)
    assert "approved_at" not in fm_before

    status, body = act(port, key=KEY, report_id=42, action="approve")
    assert status == 200 and body["ok"]

    after = (cfg.status_dir / "bug-42.md").read_text()
    fm_after, body_after = parse_frontmatter(after)
    # Approve stamped it.
    assert "approved_at" in fm_after
    # ISO-8601 UTC — parseable back to a timezone-aware datetime.
    ts = datetime.fromisoformat(fm_after["approved_at"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None
    # Every other frontmatter field preserved verbatim.
    for key, value in fm_before.items():
        assert fm_after[key] == value, f"field {key} mutated by approve"
    # Body preserved verbatim.
    assert body_after == body_before


def test_reject_does_not_write_approved_at(site):
    """Deny leaves the bug file untouched — only the surface is written.

    The tile stays in ``waiting-on-review`` until Speaking bounces the
    PR on GitHub and rewrites ``status``; stamping ``approved_at`` on
    reject would silently flip it into the chat-only in-progress strip.
    """
    cfg, port = site
    write_bug(cfg.status_dir, 7)
    before = (cfg.status_dir / "bug-7.md").read_text()

    status, body = act(
        port, key=KEY, report_id=7, action="reject", notes="lint fails on tests/"
    )
    assert status == 200

    after = (cfg.status_dir / "bug-7.md").read_text()
    # Byte-for-byte identical — reject is a pure surface drop.
    assert after == before
    fm_after, _ = parse_frontmatter(after)
    assert "approved_at" not in fm_after


# ---------------------------------------------------------------- collect_bugs promotion


def test_collect_bugs_keeps_approved_in_progress(tmp_path):
    """An ``in-progress`` bug with ``pr_url`` AND ``approved_at`` stays
    in the ``in-progress`` bucket — it does NOT flip back to
    ``waiting-on-review`` after Jason has already approved.
    """
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status,
        42,
        status="in-progress",
        approved_at="2026-11-09T12:34:56+00:00",
    )

    buckets, _ = collect_bugs(status)
    in_progress_ids = [b["report_id"] for b in buckets["in-progress"]]
    waiting_ids = [b["report_id"] for b in buckets["waiting-on-review"]]
    assert 42 in in_progress_ids
    assert 42 not in waiting_ids
    # The approved_at timestamp is projected onto the bucket dict so
    # downstream renderers (and the frontend) can see it.
    bug = next(b for b in buckets["in-progress"] if b["report_id"] == 42)
    assert bug["approved_at"] == "2026-11-09T12:34:56+00:00"


def test_collect_bugs_promotes_unapproved_pr_bug_to_waiting_on_review(tmp_path):
    """Existing behaviour unchanged: ``in-progress`` + ``pr_url`` with
    NO ``approved_at`` still gets promoted into ``waiting-on-review``.
    """
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 99, status="in-progress")  # pr_url set by default, no approved_at

    buckets, _ = collect_bugs(status)
    in_progress_ids = [b["report_id"] for b in buckets["in-progress"]]
    waiting_ids = [b["report_id"] for b in buckets["waiting-on-review"]]
    assert 99 in waiting_ids
    assert 99 not in in_progress_ids


# ---------------------------------------------------------------- _row_actions shape


def test_row_actions_in_progress_is_chat_only():
    """``_row_actions`` returns a single Chat descriptor for in-progress.

    After approve demotes a tile to in-progress, the only steering
    surface Jason gets is Chat — no approve/deny on in-progress rows.
    """
    from status_site.sources.bugs import _row_actions

    actions = _row_actions("in-progress", 42)
    assert len(actions) == 1
    (chat,) = actions
    assert chat["action"] == "bug.chat"
    assert chat["label"] == "Chat"
    assert chat["variant"] == "chat"
    assert chat["report_id"] == 42


def test_row_actions_waiting_on_review_is_triple():
    """Existing waiting-on-review behaviour unchanged: approve/deny/chat."""
    from status_site.sources.bugs import _row_actions

    actions = _row_actions("waiting-on-review", 7)
    assert [a["action"] for a in actions] == ["bug.approve", "bug.deny", "bug.chat"]
    assert [a["label"] for a in actions] == ["Approve", "Deny", "Chat"]
    assert all(a["report_id"] == 7 for a in actions)


def test_row_actions_other_buckets_are_empty():
    """Every other bucket still returns ``[]`` — no action strip."""
    from status_site.sources.bugs import _row_actions

    for bucket in ("open", "fixed", "wontfix", "dispatch-failed"):
        assert _row_actions(bucket, 1) == []
