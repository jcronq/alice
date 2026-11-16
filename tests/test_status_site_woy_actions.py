"""Slice-2 WoY Accept/Reject/Steer + title_slug + migration + dismissal.

Covers the pieces status-site.py grew in Slice-2 (#881 follow-up):

- ``title_slug`` collapses ``[text](url)`` before slugifying.
- ``migrate_stale_slugs`` renames files written under the pre-fix slug.
- ``needs_approval_slugs`` flags only PR-URL-bearing hand-zone items.
- ``handle_woy_action`` merges on PR items, dismisses on non-PR items.
- ``handle_woy_steer`` appends a ``Steer:`` thread entry without dismiss.
- ``filter_dismissed_hand_zone`` + ``filter_dismissed_asks`` drop items
  from the next render once a dismissal lands.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from status_site import server as S
from status_site.history import (
    legacy_title_slug,
    migrate_stale_slugs,
    title_slug,
)


# ---------------------------------------------------------------- title_slug


def test_title_slug_collapses_inline_links():
    # Slice-1 title form that broke slug parity: bold with an inline
    # link whose text is a reference ``([#641](URL))``.
    t = "Review draft PR ([#641](https://github.com/jcronq/alice/pull/641))"
    assert title_slug(t) == "review-draft-pr-641"
    # Trailing ``:``/``.`` stripped same as before.
    assert title_slug("Ship Slice 2 ([#881](https://ex.com/x/y/pull/881)):") == "ship-slice-2-881"
    # No link → unchanged relative to the old algorithm.
    assert title_slug("just a plain bold title") == "just-a-plain-bold-title"


def test_legacy_title_slug_keeps_pre_slice2_shape():
    t = "Review draft PR ([#641](https://github.com/jcronq/alice/pull/641))"
    legacy = legacy_title_slug(t)
    assert legacy != title_slug(t)
    # Legacy slug smears the URL (what Slice-1 wrote to disk).
    assert "github" in legacy or "https" in legacy


def _write_page(d: Path, md: str) -> None:
    (d / "index.md").write_text(md)


# ---------------------------------------------------------------- migration


def test_migrate_stale_slugs_renames_stale_history(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    md = (
        "## Waiting on you\n"
        "- [ ] **Review draft PR ([#641](https://example.com/x/y/pull/641))** some body\n"
    )
    _write_page(d, md)
    (d / "item-history").mkdir()
    (d / "threads").mkdir()
    old = legacy_title_slug(
        "Review draft PR ([#641](https://example.com/x/y/pull/641))"
    )
    new = title_slug(
        "Review draft PR ([#641](https://example.com/x/y/pull/641))"
    )
    assert old != new
    (d / "item-history" / f"{old}.jsonl").write_text('{"type":"appeared"}\n')
    (d / "threads" / f"{old}.jsonl").write_text('{"from":"alice"}\n')
    n = migrate_stale_slugs(d)
    assert n == 2
    assert (d / "item-history" / f"{new}.jsonl").exists()
    assert (d / "threads" / f"{new}.jsonl").exists()
    assert not (d / "item-history" / f"{old}.jsonl").exists()
    # Second run is a no-op.
    assert migrate_stale_slugs(d) == 0


def test_migrate_stale_slugs_handles_state_rename(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    md = (
        "## Waiting on you\n"
        "- [ ] **Ship ([#2](https://example.com/x/y/pull/2))**\n"
    )
    _write_page(d, md)
    (d / "item-history").mkdir()
    old = legacy_title_slug("Ship ([#2](https://example.com/x/y/pull/2))")
    new = title_slug("Ship ([#2](https://example.com/x/y/pull/2))")
    (d / "item-history" / f"{old}.jsonl").write_text('{"type":"appeared"}\n')
    (d / ".item-state.json").write_text(
        json.dumps({"items": {old: {"page": "index"}}})
    )
    migrate_stale_slugs(d)
    state = json.loads((d / ".item-state.json").read_text())
    assert new in state["items"]
    assert old not in state["items"]


def test_migrate_stale_slugs_rekeys_state_tombstones(tmp_path):
    """Tombstone pass: items the hand-zone no longer carries but
    which still live in ``.item-state.json`` get their legacy slug
    rekeyed and their history file renamed.
    """
    d = tmp_path / "status"
    d.mkdir()
    # No bold-titled WoY item on any page — forces the tombstone
    # path (page-scan yields no pairs).
    (d / "index.md").write_text("## Today\n")
    (d / "item-history").mkdir()
    title = "Old PR ([#99](https://example.com/x/y/pull/99))"
    legacy = legacy_title_slug(title)
    new = title_slug(title)
    assert legacy != new
    (d / "item-history" / f"{legacy}.jsonl").write_text('{"type":"appeared"}\n')
    (d / ".item-state.json").write_text(
        json.dumps({
            "items": {
                legacy: {"title": title, "gone": True, "page": "index"},
            }
        })
    )
    n = migrate_stale_slugs(d)
    assert n == 1
    assert (d / "item-history" / f"{new}.jsonl").exists()
    state = json.loads((d / ".item-state.json").read_text())
    assert new in state["items"]
    assert legacy not in state["items"]


def test_migrate_stale_slugs_preserves_dest_when_both_exist(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    md = "## Waiting on you\n- [ ] **Ship ([#5](https://ex.com/x/y/pull/5))**\n"
    _write_page(d, md)
    (d / "item-history").mkdir()
    old = legacy_title_slug("Ship ([#5](https://ex.com/x/y/pull/5))")
    new = title_slug("Ship ([#5](https://ex.com/x/y/pull/5))")
    (d / "item-history" / f"{old}.jsonl").write_text('{"legacy": true}\n')
    (d / "item-history" / f"{new}.jsonl").write_text('{"new": true}\n')
    n = migrate_stale_slugs(d)
    assert n == 0  # dest exists, source preserved
    assert (d / "item-history" / f"{old}.jsonl").exists()
    # The newer file isn't overwritten.
    assert json.loads((d / "item-history" / f"{new}.jsonl").read_text()) == {"new": True}


# ---------------------------------------------------------------- needs_approval


def test_needs_approval_slugs_flags_pr_urls_only():
    md = (
        "- [ ] **Merge draft PR ([#641](https://github.com/jcronq/alice/pull/641))** blurb\n"
        "- [ ] **Plain ask with no PR** some body\n"
        "- [ ] **Issue link ([#55](https://github.com/jcronq/alice/issues/55))** not a PR\n"
    )
    out = S.needs_approval_slugs(md)
    assert out == ["merge-draft-pr-641"]


def test_needs_approval_slugs_empty_zone_returns_empty_list():
    assert S.needs_approval_slugs("") == []
    assert S.needs_approval_slugs("No list items at all.") == []


def test_detect_pr_rejects_lookalike_domain():
    # ``evilgithub.com`` should NOT match; only real github.com prefixed
    # at a word boundary.
    assert S._detect_pr("evilgithub.com/attacker/repo/pull/7") is None
    assert S._detect_pr("see jcronq/alice#55 https://github.com/jcronq/alice/pull/9") == ("jcronq/alice", 9)
    # Scheme-prefixed form with surrounding text also matches.
    assert S._detect_pr("cf. https://github.com/a/b/pull/12 for context") == ("a/b", 12)
    # Trailing digit-adjacent characters don't truncate the number.
    assert S._detect_pr("https://github.com/a/b/pull/12345broken") is None
    # No pull marker at all: no match.
    assert S._detect_pr("https://github.com/a/b/issues/12") is None


# ---------------------------------------------------------------- dismissal


def test_filter_dismissed_hand_zone_drops_matching_items():
    md = (
        "## Waiting on you\n\n"
        "- [ ] **Alpha task** body line\n"
        "  - sub bullet should drop too\n"
        "- [ ] **Bravo task** another body\n"
    )
    out = S.filter_dismissed_hand_zone(md, {"alpha-task"})
    assert "Alpha task" not in out
    assert "sub bullet" not in out
    assert "Bravo task" in out
    # No dismissals → pass-through.
    assert S.filter_dismissed_hand_zone(md, set()) == md


def test_filter_dismissed_asks_matches_by_id_or_title():
    rows = [
        {"id": "ask-1", "text": "Just a plain ask", "urgency": "TODAY"},
        {"id": "ask-2", "text": "**Named ask** with body", "urgency": "NOW"},
        {"id": "ask-3", "text": "Keeps running", "urgency": "TODAY"},
    ]
    out = S.filter_dismissed_asks(rows, {"ask-1", "named-ask"})
    assert [r["id"] for r in out] == ["ask-3"]


# ---------------------------------------------------------------- handle_woy_action


def _mk_cfg(tmp_path: Path, *, md: str = "", merge_result=None) -> S.Config:
    """Build a minimal Config stand-in for handle_woy_action tests.
    We can't use S.Config.from_env (reads STATUS_DIR / KEY envs with
    side effects on other tests), so synthesize a duck-typed object.
    """
    d = tmp_path / "status"
    d.mkdir()
    if md:
        _write_page(d, md)
    lock = threading.Lock()

    class _C:
        status_dir = d
        threads_dir = d / "threads"
        reply_log = d / ".replies.jsonl"
        key = "k"
    cfg = _C()
    cfg.lock = lock
    if merge_result is not None:
        cfg._gh_pr_merge_runner = lambda repo, number: merge_result
    return cfg


def _pr_md(slug_title: str, pr_url: str) -> str:
    return (
        "## Waiting on you\n"
        f"- [ ] **{slug_title} ({pr_url})** description tail\n"
    )


def test_handle_woy_action_bad_key_raises_403(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_action(cfg, {"key": "nope", "slug": "x", "action": "accept"})
    assert e.value.code == 403


def test_handle_woy_action_bad_action_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_action(cfg, {"key": "k", "slug": "x", "action": "merge"})
    assert e.value.code == 400


def test_handle_woy_action_reject_dismisses_without_merge(tmp_path):
    md = "## Waiting on you\n- [ ] **Ship it** some body\n"
    cfg = _mk_cfg(tmp_path, md=md)
    out = S.handle_woy_action(cfg, {"key": "k", "slug": "ship-it", "action": "reject"})
    assert out["ok"] is True
    assert out["dismissed"] is True
    assert out["thread_message"] == "Rejected"
    # Dismissal row written.
    disp = (cfg.status_dir / S.WOY_DISMISSED_FILE).read_text().splitlines()
    assert any(json.loads(ln)["slug"] == "ship-it" for ln in disp)
    # Thread row written.
    tr = (cfg.threads_dir / "ship-it.jsonl").read_text().splitlines()
    assert len(tr) == 1
    rec = json.loads(tr[0])
    assert rec["action"] == "reject"
    assert rec["text"] == "Rejected"


def test_handle_woy_action_accept_non_pr_dismisses(tmp_path):
    md = "## Waiting on you\n- [ ] **Not a PR** some body\n"
    cfg = _mk_cfg(tmp_path, md=md)
    out = S.handle_woy_action(cfg, {"key": "k", "slug": "not-a-pr", "action": "accept"})
    assert out["dismissed"] is True
    assert out["is_pr"] is False
    assert out["thread_message"] == "Accepted"


def _green_status():
    import subprocess
    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"statusCheckRollup": [{"conclusion": "SUCCESS", "name": "build"}]}),
        stderr="",
    )


def _red_status():
    import subprocess
    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"statusCheckRollup": [{"conclusion": "FAILURE", "name": "tests"}]}),
        stderr="",
    )


def test_handle_woy_action_accept_pr_calls_runner_and_merges(tmp_path):
    md = _pr_md("Draft PR", "https://github.com/jcronq/alice/pull/641")
    import subprocess
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="merged\n", stderr="")
    cfg = _mk_cfg(tmp_path, md=md, merge_result=ok)
    cfg._gh_pr_status_runner = lambda repo, number: _green_status()
    slug = title_slug("Draft PR (https://github.com/jcronq/alice/pull/641)")
    out = S.handle_woy_action(cfg, {"key": "k", "slug": slug, "action": "accept"})
    assert out["is_pr"] is True
    assert out["merged"] is True
    assert out["dismissed"] is True
    assert "Accepted — merged" in out["thread_message"]


def test_handle_woy_action_accept_pr_refuses_merge_on_red_ci(tmp_path):
    md = _pr_md("Draft PR", "https://github.com/jcronq/alice/pull/641")
    cfg = _mk_cfg(tmp_path, md=md)
    # Merge runner MUST NOT be called when CI is red.
    merge_calls: list[tuple[str, int]] = []
    cfg._gh_pr_status_runner = lambda repo, number: _red_status()
    cfg._gh_pr_merge_runner = lambda repo, number: (
        merge_calls.append((repo, number))
        or _green_status()  # would return "success" if called — must not be
    )
    slug = title_slug("Draft PR (https://github.com/jcronq/alice/pull/641)")
    out = S.handle_woy_action(cfg, {"key": "k", "slug": slug, "action": "accept"})
    assert out["merged"] is False
    assert out["dismissed"] is False
    assert merge_calls == []  # merge never attempted
    assert "red checks" in (out.get("error") or "")


def test_handle_woy_action_accept_pr_refuses_merge_on_pending_ci(tmp_path):
    import subprocess
    md = _pr_md("Draft PR", "https://github.com/jcronq/alice/pull/641")
    cfg = _mk_cfg(tmp_path, md=md)
    pending = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"statusCheckRollup": [{"status": "IN_PROGRESS", "name": "tests"}]}),
        stderr="",
    )
    merge_calls: list[tuple[str, int]] = []
    cfg._gh_pr_status_runner = lambda repo, number: pending
    cfg._gh_pr_merge_runner = lambda repo, number: (
        merge_calls.append((repo, number))
        or subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    slug = title_slug("Draft PR (https://github.com/jcronq/alice/pull/641)")
    out = S.handle_woy_action(cfg, {"key": "k", "slug": slug, "action": "accept"})
    assert merge_calls == []
    assert "pending" in (out.get("error") or "")


def test_handle_woy_action_accept_pr_leaves_item_on_merge_failure(tmp_path):
    md = _pr_md("Draft PR", "https://github.com/jcronq/alice/pull/641")
    import subprocess
    fail = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="branch protection blocked\n"
    )
    cfg = _mk_cfg(tmp_path, md=md, merge_result=fail)
    cfg._gh_pr_status_runner = lambda repo, number: _green_status()
    slug = title_slug("Draft PR (https://github.com/jcronq/alice/pull/641)")
    out = S.handle_woy_action(cfg, {"key": "k", "slug": slug, "action": "accept"})
    assert out["merged"] is False
    assert out["dismissed"] is False
    assert "Accept failed" in out["thread_message"]
    # Dismissal file NOT written.
    assert not (cfg.status_dir / S.WOY_DISMISSED_FILE).exists()


def test_find_waiting_item_restricts_to_woy_pages(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    # Identical title in Done-today (NOT WoY) and in WoY.
    (d / "done-today.md").write_text(
        "## Merged\n- [x] **Ship Slice 2** finished\n"
    )
    (d / "waiting-on-you.md").write_text(
        "- [ ] **Ship Slice 2** approve the PR\n"
    )
    item = S._find_waiting_item(d, "ship-slice-2")
    assert item is not None
    assert item["page"] == "waiting-on-you"


def test_find_waiting_item_ignores_non_woy_section_on_index(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    (d / "index.md").write_text(
        "## Done today\n- [x] **Ship Slice 2** merged\n"
        "## Waiting on you\n- [ ] **Open PR** needs review\n"
    )
    assert S._find_waiting_item(d, "ship-slice-2") is None
    assert S._find_waiting_item(d, "open-pr") is not None


def test_filter_dismissed_hand_zone_preserves_headings_after_dismissed(tmp_path):
    md = (
        "## WoY\n"
        "- [ ] **Alpha task** body\n"
        "## Next section\n"
        "- [ ] **Beta task** body\n"
    )
    out = S.filter_dismissed_hand_zone(md, {"alpha-task"})
    assert "## Next section" in out
    assert "Alpha task" not in out
    assert "Beta task" in out


# ---------------------------------------------------------------- handle_woy_steer


def test_handle_woy_steer_appends_to_thread(tmp_path):
    md = "## Waiting on you\n- [ ] **Steerable** body\n"
    cfg = _mk_cfg(tmp_path, md=md)
    out = S.handle_woy_steer(cfg, {"key": "k", "slug": "steerable", "text": "try the other repo"})
    assert out == {"ok": True, "slug": "steerable", "action": "steer"}
    tr = (cfg.threads_dir / "steerable.jsonl").read_text().splitlines()
    assert len(tr) == 1
    rec = json.loads(tr[0])
    assert rec["from"] == "jason"
    assert rec["action"] == "steer"
    assert rec["text"].startswith("Steer: try the other repo")


def test_handle_woy_steer_rejects_empty(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_steer(cfg, {"key": "k", "slug": "x", "text": "  "})
    assert e.value.code == 400


def test_handle_woy_steer_rejects_overlong(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_steer(cfg, {"key": "k", "slug": "x", "text": "x" * (S.WOY_STEER_MAX + 1)})
    assert e.value.code == 413


def test_handle_woy_steer_does_not_dismiss(tmp_path):
    md = "## Waiting on you\n- [ ] **Steerable** body\n"
    cfg = _mk_cfg(tmp_path, md=md)
    S.handle_woy_steer(cfg, {"key": "k", "slug": "steerable", "text": "nudge"})
    # No dismissal file — item still rendered.
    assert not (cfg.status_dir / S.WOY_DISMISSED_FILE).exists()


def test_handle_woy_action_releases_lock_during_subprocess(tmp_path):
    """Regression test for the lock-held-around-subprocess finding —
    we assert cfg.lock is NOT held when the merge runner is invoked.
    """
    import subprocess
    md = _pr_md("Draft PR", "https://github.com/jcronq/alice/pull/9")
    cfg = _mk_cfg(tmp_path, md=md)
    cfg._gh_pr_status_runner = lambda repo, number: _green_status()
    seen_lock_held: list[bool] = []

    def _probe_runner(repo, number):
        # Try non-blocking acquire; if it succeeds the lock was free.
        got = cfg.lock.acquire(blocking=False)
        seen_lock_held.append(not got)
        if got:
            cfg.lock.release()
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    cfg._gh_pr_merge_runner = _probe_runner
    slug = title_slug("Draft PR (https://github.com/jcronq/alice/pull/9)")
    out = S.handle_woy_action(cfg, {"key": "k", "slug": slug, "action": "accept"})
    assert out["merged"] is True
    assert seen_lock_held == [False]


# ---------------------------------------------------------------- read_woy_dismissed


def test_read_woy_dismissed_tolerates_malformed_lines(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    f = d / S.WOY_DISMISSED_FILE
    f.write_text(
        'this is not json\n'
        '{"slug": "ok", "action": "accept"}\n'
        '{"not_a_slug": true}\n'
        '{"slug": "../escape", "action": "reject"}\n'  # safe_slug sanitizes
    )
    out = S.read_woy_dismissed(d)
    assert "ok" in out
    # ../escape gets sanitised to just "escape" by safe_slug.
    assert "escape" in out
