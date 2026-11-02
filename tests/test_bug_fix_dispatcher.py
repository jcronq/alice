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
    TickReport,
    _extract_captured_state,
    _extract_pr_url_from_result,
    _extract_report_text,
    _render_prompt,
    _scan_bugs,
    tick,
)


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
    p = write_bug(
        cfg.status_dir,
        7,
        status="in-progress",
        extra_front={
            "dispatched_at": "2026-11-02T17:00:00+00:00",
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
