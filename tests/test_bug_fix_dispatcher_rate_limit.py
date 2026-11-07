"""Integration tests: rate-limit gate on the bug-fix dispatcher.

Issue #786 M3. When the shared backoff file indicates a 429 is still
being retried, ``_dispatch_worker`` must defer rather than firing a
fresh ``claude -p`` spawn (which would push 15+ gh calls through the
already-throttled bucket). The deferred bug stays at ``status: open``
without ``dispatched_at``, so the next tick retries once the window
closes.
"""

from __future__ import annotations

import pathlib

import pytest

from alice_bug_fix_dispatcher import frontmatter as fm
from alice_bug_fix_dispatcher.main import DispatcherConfig, tick
from alice_forge import rate_limit as rl


# ---------------------------------------------------------------------------
# Helpers (minimal clones of the fixtures in test_bug_fix_dispatcher.py so
# this suite can stand alone)
# ---------------------------------------------------------------------------


_BODY = """# bug-{n} — {title}

**Status:** open.

## Report

> something broke

## Who / where

- Reported 2026-11-02 12:12 EST
- Build: deadbeef
"""


def _write_bug(
    status_dir: pathlib.Path, report_id: int, *, title: str = "test"
) -> pathlib.Path:
    text = (
        f"---\n"
        f"report_id: {report_id}\n"
        f'title: "{title}"\n'
        f"status: open\n"
        f"---\n\n{_BODY.format(n=report_id, title=title)}"
    )
    p = status_dir / f"bug-{report_id}.md"
    p.write_text(text)
    return p


def _cfg(tmp_path: pathlib.Path) -> DispatcherConfig:
    status = tmp_path / "status"
    status.mkdir()
    template = tmp_path / "template.md"
    template.write_text("worker for bug-{{bug_id}}\n")
    return DispatcherConfig(
        status_dir=status,
        journal_path=tmp_path / "journal.log",
        pause_sentinel=tmp_path / ".paused",
        config_json=tmp_path / "config.json",
        worker_scratch_root=tmp_path / "scratch",
        worker_prompt_template=template,
        stall_hours=2,
        index_path=tmp_path / "index.md",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_active_backoff_defers_bug_dispatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded 429 freezes bug-fix fan-out until the window elapses."""
    backoff_path = tmp_path / "backoff.json"
    monkeypatch.setenv("ALICE_GH_RATE_LIMIT_BACKOFF_PATH", str(backoff_path))

    # Record a hit (future-dated backoff_until).
    rl.record_rate_limit_hit(reason="secondary rate limit", jitter=lambda lo, hi: 0.0)

    cfg = _cfg(tmp_path)
    bug_path = _write_bug(cfg.status_dir, 42, title="under-rate-limit")

    spawn_calls: list[int] = []

    def spawn(bug_id: int, scratch: pathlib.Path, prompt: str) -> int:
        spawn_calls.append(bug_id)
        return 1000

    rep = tick(cfg, spawn=spawn)

    # Not dispatched, but also not flagged as spawn-failed in a way that
    # loses the record.
    assert spawn_calls == []
    assert 42 not in rep.dispatched
    # Status still open — no dispatched_at written — so next tick retries.
    reparsed, _body, _raw = fm.parse(bug_path.read_text())
    assert reparsed["status"] == "open"
    assert "dispatched_at" not in reparsed
    # Journal recorded the reason.
    journal_text = cfg.journal_path.read_text()
    assert "rate-limit gate deferred" in journal_text


def test_no_backoff_file_proceeds_normally(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh host (no backoff file yet) → fail-open, dispatch proceeds."""
    backoff_path = tmp_path / "nonexistent-backoff.json"
    monkeypatch.setenv("ALICE_GH_RATE_LIMIT_BACKOFF_PATH", str(backoff_path))

    cfg = _cfg(tmp_path)
    _write_bug(cfg.status_dir, 43)

    spawn_calls: list[int] = []

    def spawn(bug_id: int, scratch: pathlib.Path, prompt: str) -> int:
        spawn_calls.append(bug_id)
        return 1001

    rep = tick(cfg, spawn=spawn)
    assert rep.dispatched == [43]
    assert spawn_calls == [43]
    # No backoff file was created merely by the pre-flight read.
    assert not backoff_path.exists()


def test_cleared_backoff_unfreezes_dispatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the window elapses (``clear_backoff``), dispatch resumes."""
    backoff_path = tmp_path / "backoff.json"
    monkeypatch.setenv("ALICE_GH_RATE_LIMIT_BACKOFF_PATH", str(backoff_path))

    rl.record_rate_limit_hit(reason="x", jitter=lambda lo, hi: 0.0)
    rl.clear_backoff()

    cfg = _cfg(tmp_path)
    _write_bug(cfg.status_dir, 44)

    spawn_calls: list[int] = []

    def spawn(bug_id: int, scratch: pathlib.Path, prompt: str) -> int:
        spawn_calls.append(bug_id)
        return 1002

    rep = tick(cfg, spawn=spawn)
    assert rep.dispatched == [44]
    assert spawn_calls == [44]
