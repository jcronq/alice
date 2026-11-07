"""Tests for the Modern-MUD bug-fix dispatcher.

Covers the four behaviors Jason signed off on:

* Pause gate: sentinel file + config flag; either input stops the scan.
* Open → in-progress: idempotent via ``dispatched_at``; frontmatter
  patched atomically; worker scratch dir seeded with the substituted
  prompt.
* In-progress → fixed / open / dispatch-failed: PR state poll +
  stall sweep.
* Frontmatter round-trip: unknown fields are preserved; writes are
  atomic (tmp + rename, not torn on crash).

I/O surfaces (file system, worker spawn, ``gh pr view``) are all
injectable so tests never shell out.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from typing import Any

import pytest

from alice_bug_fix_dispatcher import frontmatter as fm
from alice_bug_fix_dispatcher.main import (
    DispatcherConfig,
    _extract_captured_state,
    _extract_pr_url_from_result,
    _extract_report_text,
    _pr_is_green,
    _rebuild_waiting_block,
    _render_prompt,
    _scan_bugs,
    _post_transition,
    tick,
)
from status_site.history import title_slug as site_title_slug


# ---------------------------------------------------------------- helpers


BUG_BODY = """# bug-{n} — {title}

**Status:** open.

## Report

> cake-box didn't swap for the empty tin after giving to michael

## Who / where

- Reported 2026-11-02 12:12 EST (17:12 UTC)
- World: howls_moving_castle
- Build: 5ea667a61aaab3315ade5ddf046abf04b8565ee9

## Quest state at report

- `cesari_parcel`: active
- Counters: `michael_trust = 2`

## Likely fix surface

Michael's accept-the-box node needs the inventory swap atomically.
"""


def write_bug(
    status_dir: pathlib.Path,
    report_id: int,
    *,
    status: str = "open",
    title: str = "placeholder title",
    extra_front: dict[str, str] | None = None,
    body: str | None = None,
) -> pathlib.Path:
    front_lines = ["---", f"report_id: {report_id}", f'title: "{title}"', f"status: {status}"]
    for k, v in (extra_front or {}).items():
        front_lines.append(f"{k}: {v}")
    front_lines.append("---")
    text = "\n".join(front_lines) + "\n\n" + (body or BUG_BODY.format(n=report_id, title=title))
    p = status_dir / f"bug-{report_id}.md"
    p.write_text(text)
    return p


def _make_cfg(tmp_path: pathlib.Path, *, template: str | None = None) -> DispatcherConfig:
    status = tmp_path / "status"
    status.mkdir()
    template_path = tmp_path / "template.md"
    template_path.write_text(template or (
        "worker for bug-{{bug_id}} ({{title}})\n"
        "report: {{report_text}}\n"
        "state: {{captured_state}}\n"
        "build: {{build_sha}}\n"
    ))
    return DispatcherConfig(
        status_dir=status,
        journal_path=tmp_path / "journal.log",
        pause_sentinel=tmp_path / ".paused",
        config_json=tmp_path / "config.json",
        worker_scratch_root=tmp_path / "scratch",
        worker_prompt_template=template_path,
        stall_hours=2,
        index_path=tmp_path / "index.md",
    )


# ---------------------------------------------------------------- frontmatter


def test_frontmatter_roundtrip_preserves_unknown(tmp_path):
    """A hand-added field (``summary``, future fields) survives a dispatcher write."""
    p = tmp_path / "bug-7.md"
    p.write_text(
        "---\n"
        "report_id: 7\n"
        'title: "has colon: tricky"\n'
        "status: open\n"
        "summary: human-written summary\n"
        "---\n\n# bug-7 — body\n"
    )
    merged = fm.update(p, {"status": "in-progress", "dispatched_at": "2026-11-02T17:00:00+00:00"})
    assert merged["status"] == "in-progress"
    # Unknown field preserved.
    assert "human-written summary" in p.read_text()
    # New field appended (not inserted at head), so existing order holds.
    text = p.read_text()
    assert text.index("report_id") < text.index("dispatched_at")
    # Values with colons stay quoted on re-parse.
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["title"] == "has colon: tricky"
    assert reparsed["dispatched_at"] == "2026-11-02T17:00:00+00:00"


def test_frontmatter_update_drops_none_values(tmp_path):
    p = tmp_path / "bug-9.md"
    p.write_text(
        "---\n"
        "report_id: 9\n"
        "status: in-progress\n"
        "dispatched_at: 2026-11-02T17:00:00+00:00\n"
        "pr_url: https://github.com/jcronq/modern-mud/pull/42\n"
        "---\n\nbody\n"
    )
    fm.update(p, {"status": "open", "dispatched_at": None, "pr_url": None})
    text = p.read_text()
    assert "dispatched_at" not in text
    assert "pr_url" not in text
    assert "status: open" in text


def test_frontmatter_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "bug-3.md"
    p.write_text("---\nreport_id: 3\nstatus: open\n---\n\nbody\n")
    fm.update(p, {"status": "in-progress"})
    # No lingering tempfile.
    assert sorted(x.name for x in tmp_path.iterdir()) == ["bug-3.md"]


# ---------------------------------------------------------------- scan + extract


def test_scan_bugs_sorted_ascending(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 2)
    write_bug(status, 1)
    write_bug(status, 10)
    out = _scan_bugs(status)
    assert [row[0] for row in out] == [1, 2, 10]


def test_extract_report_text_pulls_blockquote():
    body = (
        "# bug-2 — title\n\n"
        "## Report\n\n"
        "> first line\n"
        "> second line\n\n"
        "## Who / where\n\nxyz\n"
    )
    assert _extract_report_text(body) == "first line\nsecond line"


def test_extract_captured_state_starts_at_who_where():
    body = "# h\n\n## Report\n\n> x\n\n## Who / where\n\n- loc\n"
    out = _extract_captured_state(body)
    assert out.startswith("## Who / where")


def test_extract_pr_url_from_result_envelope(tmp_path):
    r = tmp_path / "result.json"
    r.write_text(
        '{"type":"result","result":"{\\"pr_url\\":\\"https://github.com/jcronq/modern-mud/pull/42\\",\\"commit_sha\\":\\"abc\\"}"}'
    )
    assert (
        _extract_pr_url_from_result(r)
        == "https://github.com/jcronq/modern-mud/pull/42"
    )


def test_extract_pr_url_from_result_regex_fallback(tmp_path):
    r = tmp_path / "result.json"
    r.write_text("worker crashed but https://github.com/jcronq/modern-mud/pull/42 was created\n")
    assert (
        _extract_pr_url_from_result(r)
        == "https://github.com/jcronq/modern-mud/pull/42"
    )


def test_render_prompt_substitutes_all_placeholders():
    template = "a {{bug_id}} b {{title}} c {{report_text}} d {{captured_state}} e {{build_sha}}"
    out = _render_prompt(
        template,
        bug_id=7,
        title="t",
        report_text="rep",
        captured_state="state",
        build_sha="deadbeef",
    )
    assert out == "a 7 b t c rep d state e deadbeef"


# ---------------------------------------------------------------- pause gate


def test_tick_skipped_when_sentinel_present(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.pause_sentinel.write_text("paused\n")
    write_bug(cfg.status_dir, 1, status="open")

    spawned = []

    def spawn(bug_id, scratch, prompt):
        spawned.append(bug_id)
        return 999

    rep = tick(cfg, spawn=spawn)
    assert rep.paused is True
    assert rep.scanned == 0
    assert spawned == []
    log = cfg.journal_path.read_text()
    assert "paused" in log


def test_tick_skipped_when_config_flag_set(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.config_json.write_text('{"bug_fix_dispatcher": {"wave_a_signoff_pending": true}}')
    write_bug(cfg.status_dir, 1, status="open")

    spawned = []
    rep = tick(cfg, spawn=lambda *a: spawned.append(a[0]) or 999)
    assert rep.paused is True
    assert rep.scanned == 0
    assert spawned == []


def test_tick_resumes_when_sentinel_removed(tmp_path):
    cfg = _make_cfg(tmp_path)
    # Start paused.
    cfg.pause_sentinel.write_text("paused\n")
    write_bug(cfg.status_dir, 1, status="open")
    tick(cfg, spawn=lambda *a: 999)

    # Remove and tick again.
    cfg.pause_sentinel.unlink()
    rep = tick(cfg, spawn=lambda *a: 999)
    assert rep.paused is False
    assert rep.scanned == 1
    assert rep.dispatched == [1]


# ---------------------------------------------------------------- dispatch


def test_open_bug_gets_dispatched_and_frontmatter_patched(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(cfg.status_dir, 7, status="open", title="give-cakes")

    captured_prompts = []

    def spawn(bug_id, scratch, prompt):
        captured_prompts.append((bug_id, scratch, prompt))
        return 4242

    rep = tick(cfg, spawn=spawn)
    assert rep.dispatched == [7]
    assert captured_prompts and captured_prompts[0][0] == 7
    # Prompt was substituted and scratch dir seeded.
    scratch_dir = cfg.worker_scratch_root / "bug-7"
    assert (scratch_dir / "prompt.md").is_file()
    prompt_text = (scratch_dir / "prompt.md").read_text()
    assert "bug-7" in prompt_text
    assert "give-cakes" in prompt_text
    # Frontmatter now in-progress with dispatched_at + worker_task_id.
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"
    assert reparsed["worker_task_id"] == "pid:4242"
    assert reparsed["dispatched_at"]


def test_dispatch_idempotency_dispatched_at_blocks_respawn(tmp_path):
    """A bug whose ``status`` was flipped to ``in-progress`` on a prior
    tick must never re-trigger a spawn, even if the file is scanned again."""
    cfg = _make_cfg(tmp_path)
    # Recent enough to stay inside the stall TTL, otherwise the stall
    # sweep flips the bug to dispatch-failed instead of leaving it alone.
    recent = (
        (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5))
        .replace(microsecond=0)
        .isoformat()
    )
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": recent,
            "worker_task_id": "pid:4242",
        },
    )

    calls = []

    def spawn(bug_id, scratch, prompt):
        calls.append(bug_id)
        return 9999

    tick(cfg, spawn=spawn)
    assert calls == []
    # Status still in-progress.
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"


def test_dispatch_failure_leaves_status_open(tmp_path):
    """Spawn-failure path: dispatcher must not pretend it succeeded."""
    cfg = _make_cfg(tmp_path)
    p = write_bug(cfg.status_dir, 3, status="open")

    rep = tick(cfg, spawn=lambda *a: None)
    assert rep.dispatched == []
    assert any(sr[0] == 3 and "spawn-failed" in sr[1] for sr in rep.skipped)
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "open"
    assert "dispatched_at" not in reparsed


# ---------------------------------------------------------------- adopt pr_url + stall


def test_in_progress_adopts_pr_url_from_result_json(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:1",
        },
    )
    scratch = cfg.worker_scratch_root / "bug-7"
    scratch.mkdir(parents=True)
    (scratch / "result.json").write_text(
        '{"pr_url": "https://github.com/jcronq/modern-mud/pull/42", "commit_sha": "abc"}'
    )

    rep = tick(cfg, spawn=lambda *a: 999)
    assert rep.adopted_pr_url == [7]
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["pr_url"] == "https://github.com/jcronq/modern-mud/pull/42"


def test_stall_sweep_flips_to_dispatch_failed(tmp_path):
    cfg = _make_cfg(tmp_path)
    # Dispatched 3h ago, no result.json → stall.
    stale = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=3)).replace(microsecond=0).isoformat()
    p = write_bug(
        cfg.status_dir,
        9,
        status="in-progress",
        extra_front={
            "dispatched_at": stale,
            "worker_task_id": "pid:1",
        },
    )
    rep = tick(cfg, spawn=lambda *a: 999)
    assert rep.stalled == [9]
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "dispatch-failed"
    assert reparsed["last_dispatch_failed_at"]


def test_fresh_in_progress_no_result_json_no_stall(tmp_path):
    cfg = _make_cfg(tmp_path)
    fresh = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)).replace(microsecond=0).isoformat()
    p = write_bug(
        cfg.status_dir,
        9,
        status="in-progress",
        extra_front={"dispatched_at": fresh, "worker_task_id": "pid:1"},
    )
    rep = tick(cfg, spawn=lambda *a: 999)
    assert rep.stalled == []
    reparsed, _body, _raw = fm.parse(p.read_text())
    # Still in-progress — not stale yet.
    assert reparsed["status"] == "in-progress"


# ---------------------------------------------------------------- PR poll


def test_merged_pr_flips_to_fixed(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:1",
            "pr_url": "https://github.com/jcronq/modern-mud/pull/42",
        },
    )

    def fake_pr_view(url: str) -> dict[str, Any]:
        return {
            "state": "MERGED",
            "mergeCommit": {"oid": "deadbeef0001"},
            "url": url,
        }

    rep = tick(cfg, spawn=lambda *a: 999, gh_pr_view=fake_pr_view)
    assert rep.fixed == [7]
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "fixed"
    assert reparsed["fix_commit"] == "deadbeef0001"
    assert reparsed["fix_pr"] == "https://github.com/jcronq/modern-mud/pull/42"


def test_closed_unmerged_pr_reverts_to_open_and_clears_dispatched_at(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:1",
            "pr_url": "https://github.com/jcronq/modern-mud/pull/42",
        },
    )

    def fake_pr_view(url: str) -> dict[str, Any]:
        return {"state": "CLOSED", "url": url}

    rep = tick(cfg, spawn=lambda *a: 999, gh_pr_view=fake_pr_view)
    assert rep.reopened == [7]
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "open"
    assert "dispatched_at" not in reparsed
    assert "worker_task_id" not in reparsed
    assert "pr_url" not in reparsed
    assert reparsed["last_dispatch_failed_at"]
    assert reparsed["last_failed_pr"] == "https://github.com/jcronq/modern-mud/pull/42"


def test_open_pr_leaves_status_in_progress(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:1",
            "pr_url": "https://github.com/jcronq/modern-mud/pull/42",
        },
    )

    def fake_pr_view(url: str) -> dict[str, Any]:
        return {"state": "OPEN", "url": url}

    rep = tick(cfg, spawn=lambda *a: 999, gh_pr_view=fake_pr_view)
    assert rep.fixed == [] and rep.reopened == []
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"
    assert reparsed["last_check_at"]


# ---------------------------------------------------------------- terminal states


@pytest.mark.parametrize("terminal", ["fixed", "wontfix", "dispatch-failed"])
def test_terminal_statuses_are_noop(tmp_path, terminal):
    cfg = _make_cfg(tmp_path)
    p = write_bug(cfg.status_dir, 7, status=terminal)
    before = p.read_text()
    calls = []
    tick(cfg, spawn=lambda *a: calls.append(a) or 1, gh_pr_view=lambda u: {"state": "MERGED"})
    assert calls == []
    assert p.read_text() == before


# ---------------------------------------------------------------- hand-edit guard


def test_open_with_stale_dispatched_at_is_skipped(tmp_path):
    """Hand-edit scenario: status flipped to ``open`` but ``dispatched_at`` left.

    The dispatcher must NOT silently re-spawn — Jason has to clear the
    idempotency key explicitly. Otherwise a half-rolled-back closed-
    unmerged bug would double-dispatch.
    """
    cfg = _make_cfg(tmp_path)
    p = write_bug(
        cfg.status_dir,
        5,
        status="open",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:99",
        },
    )
    calls = []
    tick(cfg, spawn=lambda *a: calls.append(a) or 1)
    assert calls == []
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "open"
    assert reparsed["dispatched_at"]


# ---------------------------------------------------------------- ready-for-review


PR_URL = "https://github.com/jcronq/modern-mud/pull/42"
GREEN = [
    {"conclusion": "SUCCESS", "status": "COMPLETED"},
    {"state": "SUCCESS"},
]
INDEX_MD = """# Status

## Running now

- [ ] something

## Waiting on you

- [ ] **PFL: confirm the payout.**

## Later

- [ ] other
"""


def _rfr_setup(tmp_path, *, reporter_in_body="cronqj", extra=None, status="in-progress"):
    cfg = _make_cfg(tmp_path)
    cfg.index_path.write_text(INDEX_MD)
    body = BUG_BODY.format(n=7, title="cake box").replace(
        "## Who / where\n", f"## Who / where\n\n- Account: {reporter_in_body}\n", 1
    )
    front = {
        "dispatched_at": "2026-11-02T17:00:00+00:00",
        "worker_task_id": "pid:1",
        "pr_url": PR_URL,
    }
    front.update(extra or {})
    p = write_bug(
        cfg.status_dir, 7, status=status, title="cake box", extra_front=front, body=body
    )
    return cfg, p


def _view(**kw):
    data = {
        "state": "OPEN",
        "isDraft": True,
        "statusCheckRollup": GREEN,
        "reviewDecision": "",
    }
    data.update(kw)
    return lambda url: data


def test_pr_is_green():
    assert _pr_is_green({"statusCheckRollup": GREEN})
    assert not _pr_is_green({"statusCheckRollup": []})
    assert not _pr_is_green({})
    assert not _pr_is_green({"statusCheckRollup": GREEN + [{"conclusion": "FAILURE"}]})
    assert not _pr_is_green({"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]})


def test_green_draft_trusted_flips_ready_and_lists_waiting_line(tmp_path):
    cfg, p = _rfr_setup(tmp_path)
    ready_calls: list[str] = []
    rep = tick(cfg, gh_pr_view=_view(), gh_pr_ready=ready_calls.append)
    assert ready_calls == [PR_URL]
    assert rep.ready == [7]
    reparsed, _b, _r = fm.parse(p.read_text())
    assert reparsed["status"] == "ready-for-review"
    index = cfg.index_path.read_text()
    assert "- [ ] Review PR #42 (bug-7: cake box)" in index
    waiting = index.index("## Waiting on you")
    assert waiting < index.index("Review PR #42") < index.index("## Later")


def test_trusted_reporter_from_frontmatter_field(tmp_path):
    cfg, p = _rfr_setup(tmp_path, reporter_in_body="stranger", extra={"reporter": "CronQJ"})
    calls: list[str] = []
    tick(cfg, gh_pr_view=_view(), gh_pr_ready=calls.append)
    assert calls == [PR_URL]


def test_trusted_reporters_overridable_in_config(tmp_path):
    cfg, p = _rfr_setup(tmp_path, reporter_in_body="friend")
    cfg.config_json.write_text('{"bug_fix_dispatcher": {"trusted_reporters": ["friend"]}}')
    calls: list[str] = []
    tick(cfg, gh_pr_view=_view(), gh_pr_ready=calls.append)
    assert calls == [PR_URL]


@pytest.mark.parametrize(
    "view_kw, reporter",
    [
        ({"statusCheckRollup": [{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]}, "cronqj"),
        ({"statusCheckRollup": [{"conclusion": "FAILURE"}]}, "cronqj"),
        ({"statusCheckRollup": []}, "cronqj"),
        ({"reviewDecision": "CHANGES_REQUESTED"}, "cronqj"),
        ({}, "stranger"),
        ({"isDraft": False}, "cronqj"),
    ],
)
def test_no_flip_when_not_eligible(tmp_path, view_kw, reporter):
    cfg, p = _rfr_setup(tmp_path, reporter_in_body=reporter)
    calls: list[str] = []
    rep = tick(cfg, gh_pr_view=_view(**view_kw), gh_pr_ready=calls.append)
    assert calls == []
    assert rep.ready == []
    reparsed, _b, _r = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"
    assert cfg.index_path.read_text() == INDEX_MD


def test_gh_pr_ready_failure_leaves_status(tmp_path):
    cfg, p = _rfr_setup(tmp_path)

    def boom(url):
        raise RuntimeError("gh down")

    rep = tick(cfg, gh_pr_view=_view(), gh_pr_ready=boom)
    assert rep.ready == []
    reparsed, _b, _r = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"


def test_ready_for_review_not_reflipped_and_merge_routes_to_fixed(tmp_path):
    cfg, p = _rfr_setup(tmp_path)
    tick(cfg, gh_pr_view=_view(), gh_pr_ready=lambda u: None)
    # Hand-edit a line into the Waiting section next to the managed block.
    index = cfg.index_path.read_text().replace(
        "- [ ] **PFL: confirm the payout.**",
        "- [ ] **PFL: confirm the payout.**\n- [ ] hand-added line",
    )
    cfg.index_path.write_text(index)

    # Still draft again + green: must not re-flip an already ready-for-review bug.
    calls: list[str] = []
    rep = tick(cfg, gh_pr_view=_view(), gh_pr_ready=calls.append)
    assert calls == [] and rep.ready == []
    assert "Review PR #42" in cfg.index_path.read_text()

    rep = tick(cfg, gh_pr_view=lambda u: {"state": "MERGED", "mergeCommit": {"oid": "abc"}})
    assert rep.fixed == [7]
    reparsed, _b, _r = fm.parse(p.read_text())
    assert reparsed["status"] == "fixed"
    final = cfg.index_path.read_text()
    assert "Review PR" not in final
    assert "bug-review" not in final
    assert "- [ ] hand-added line" in final
    assert "- [ ] **PFL: confirm the payout.**" in final
    assert "## Later" in final


def test_rebuild_waiting_block_idempotent(tmp_path):
    cfg, _p = _rfr_setup(tmp_path, status="ready-for-review")
    assert _rebuild_waiting_block(cfg.status_dir, cfg.index_path) is True
    once = cfg.index_path.read_text()
    assert once.count("<!-- bug-review -->") == 1
    assert _rebuild_waiting_block(cfg.status_dir, cfg.index_path) is False
    assert cfg.index_path.read_text() == once
    tick(cfg, gh_pr_view=lambda u: {"state": "OPEN"})
    assert cfg.index_path.read_text() == once


def test_rebuild_waiting_block_noop_without_ready_bugs_or_index(tmp_path):
    cfg = _make_cfg(tmp_path)
    assert _rebuild_waiting_block(cfg.status_dir, cfg.index_path) is False  # no index
    cfg.index_path.write_text(INDEX_MD)
    assert _rebuild_waiting_block(cfg.status_dir, cfg.index_path) is False
    assert cfg.index_path.read_text() == INDEX_MD


def test_rebuild_waiting_block_creates_section_when_missing(tmp_path):
    cfg, _p = _rfr_setup(tmp_path, status="ready-for-review")
    cfg.index_path.write_text("# Status\n\n- [ ] x\n")
    assert _rebuild_waiting_block(cfg.status_dir, cfg.index_path) is True
    text = cfg.index_path.read_text()
    assert "## Waiting on you" in text and "Review PR #42" in text
    assert text.startswith("# Status\n\n- [ ] x\n")
# ---------------------------------------------------------------- thread posts


class ReplyCollector:
    """Fake ``post_reply`` that records ``(slug, msg)`` pairs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def __call__(self, slug: str, msg: str) -> None:
        self.posts.append((slug, msg))


def test_dispatch_posts_worker_spawned(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 3, status="open", title="cake-box swap")
    posts = ReplyCollector()
    tick(cfg, spawn=lambda *a: 4242, post_reply=posts)
    assert posts.posts == [("bug-3-cake-box-swap", "worker-spawned pid:4242")]


def test_spawn_failure_posts_worker_crash(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 3, status="open", title="cake-box swap")
    posts = ReplyCollector()
    tick(cfg, spawn=lambda *a: None, post_reply=posts)
    assert posts.posts == [("bug-3-cake-box-swap", "worker crash: spawn failed")]


def test_adopting_pr_url_posts_pr_opened(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        title="door sticks",
        extra_front={"dispatched_at": "2026-11-02T17:00:00+00:00", "worker_task_id": "pid:1"},
    )
    scratch = cfg.worker_scratch_root / "bug-7"
    scratch.mkdir(parents=True)
    url = "https://github.com/jcronq/modern-mud/pull/42"
    (scratch / "result.json").write_text(f'{{"pr_url": "{url}"}}')
    posts = ReplyCollector()
    tick(cfg, post_reply=posts)
    assert posts.posts == [("bug-7-door-sticks", f"PR {url} opened, CI pending")]


def test_merge_posts_merged_sha(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        title="door sticks",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "pr_url": "https://github.com/jcronq/modern-mud/pull/42",
        },
    )
    posts = ReplyCollector()
    tick(
        cfg,
        gh_pr_view=lambda u: {"state": "MERGED", "mergeCommit": {"oid": "deadbeef0001"}},
        post_reply=posts,
    )
    assert posts.posts == [
        ("bug-7-door-sticks", "merged deadbeef0001, bug flipped to fixed")
    ]


def test_stall_posts_flagging(tmp_path):
    cfg = _make_cfg(tmp_path)
    stale = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=3)).replace(microsecond=0).isoformat()
    write_bug(
        cfg.status_dir,
        9,
        status="in-progress",
        title="door sticks",
        extra_front={"dispatched_at": stale, "worker_task_id": "pid:1"},
    )
    posts = ReplyCollector()
    tick(cfg, post_reply=posts)
    assert posts.posts == [("bug-9-door-sticks", "worker no PR after 2h, flagging")]


def test_settled_crash_posts_once(tmp_path):
    import os

    cfg = _make_cfg(tmp_path)
    fresh = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)).replace(microsecond=0).isoformat()
    write_bug(
        cfg.status_dir,
        9,
        status="in-progress",
        title="door sticks",
        extra_front={"dispatched_at": fresh, "worker_task_id": "pid:1"},
    )
    scratch = cfg.worker_scratch_root / "bug-9"
    scratch.mkdir(parents=True)
    err = scratch / "stderr.log"
    err.write_text("boom")
    old = _dt.datetime.now().timestamp() - 3600
    os.utime(err, (old, old))
    posts = ReplyCollector()
    tick(cfg, post_reply=posts)
    tick(cfg, post_reply=posts)
    assert posts.posts == [("bug-9-door-sticks", "worker crash: exited without a PR")]


def test_poll_only_ticks_do_not_post(tmp_path):
    """Open PR → only ``last_check_at`` moves; no thread chatter."""
    cfg = _make_cfg(tmp_path)
    write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "pr_url": "https://github.com/jcronq/modern-mud/pull/42",
        },
    )
    posts = ReplyCollector()
    for _ in range(3):
        tick(cfg, gh_pr_view=lambda u: {"state": "OPEN"}, post_reply=posts)
    assert posts.posts == []


def test_poster_exception_does_not_abort_tick(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(cfg.status_dir, 3, status="open")

    def boom(slug: str, msg: str) -> None:
        raise RuntimeError("thread store down")

    rep = tick(cfg, spawn=lambda *a: 4242, post_reply=boom)
    assert rep.dispatched == [3]
    reparsed, _body, _raw = fm.parse(p.read_text())
    assert reparsed["status"] == "in-progress"


def test_slug_matches_site_for_punctuated_title(tmp_path):
    title = "Cake-box: won't swap (empty tin) — Michael!"
    posts = ReplyCollector()
    _post_transition(12, title, "hi", post_reply=posts)
    assert posts.posts == [(site_title_slug(f"bug-12 — {title}"), "hi")]
    assert posts.posts[0][0] == "bug-12-cake-box-won-t-swap-empty-tin-michael"


def test_slug_falls_back_to_heading_when_frontmatter_title_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = cfg.status_dir / "bug-5.md"
    p.write_text(
        "---\nreport_id: 5\nstatus: open\n---\n\n"
        + BUG_BODY.format(n=5, title="heading only title")
    )
    posts = ReplyCollector()
    tick(cfg, spawn=lambda *a: 1, post_reply=posts)
    assert posts.posts == [("bug-5-heading-only-title", "worker-spawned pid:1")]


def test_default_poster_writes_thread_file(tmp_path, monkeypatch):
    """No injected poster → ``status-reply`` CLI appends to ``threads/<slug>.jsonl``."""
    status = tmp_path / "sd"
    monkeypatch.setenv("STATUS_DIR", str(status))
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 3, status="open", title="cake-box swap")
    tick(cfg, spawn=lambda *a: 4242)
    thread = status / "threads" / "bug-3-cake-box-swap.jsonl"
    assert "worker-spawned pid:4242" in thread.read_text()


# ---------------------------------------------------------------- depends_on gate


def _item_state(status_dir, **checked):
    import json

    (status_dir / ".item-state.json").write_text(
        json.dumps({"items": {k: {"checked": v} for k, v in checked.items()}})
    )


def test_dispatch_declines_while_dep_open_then_proceeds(tmp_path):
    cfg = _make_cfg(tmp_path)
    p = write_bug(cfg.status_dir, 9, extra_front={"depends_on": "[wave-a]"})
    _item_state(cfg.status_dir, **{"wave-a": False})
    spawned = []

    def spawn(bug_id, scratch, prompt):
        spawned.append(bug_id)
        return 1

    rep = tick(cfg, spawn=spawn)
    assert spawned == [] and rep.dispatched == []
    assert (9, "blocked") in rep.skipped
    assert "blocked by wave-a" in (tmp_path / "journal.log").read_text()
    assert fm.parse(p.read_text())[0]["status"] == "open"

    _item_state(cfg.status_dir, **{"wave-a": True})
    rep = tick(cfg, spawn=spawn)
    assert spawned == [9] and rep.dispatched == [9]


def test_dispatch_blocked_by_unknown_dep_slug(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 9, extra_front={"depends_on": "wave-typo"})
    rep = tick(cfg, spawn=lambda *a: 1)
    assert rep.dispatched == [] and (9, "blocked") in rep.skipped


def test_dispatch_dep_on_other_bug_resolves_when_fixed(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 3, status="in-progress", extra_front={"pr_url": "https://x/1"})
    write_bug(cfg.status_dir, 4, extra_front={"depends_on": "[bug-3]"})
    rep = tick(cfg, spawn=lambda *a: 1, gh_pr_view=lambda url: {"state": "OPEN"})
    assert 4 not in rep.dispatched
    write_bug(cfg.status_dir, 3, status="fixed")
    rep = tick(cfg, spawn=lambda *a: 1)
    assert rep.dispatched == [4]
