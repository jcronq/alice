"""Tests for the gh-state mirror — deferred state write/read/cleanup paths.

These are the regression tests for #253: the dispatcher kept re-surfacing
issues that Speaking had already deferred because there was no write or
read path for ``type: deferred`` and the cleanup loop would have killed
manually-created ones. The tests below pin all three fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alice_daemon import gh_state_mirror


@pytest.fixture
def gh_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level GH_STATE_DIR / LOG_FILE under tmp_path."""
    state_dir = tmp_path / "gh-state"
    state_dir.mkdir(parents=True)
    log_file = tmp_path / "inner" / "state" / "gh-state-mirror.log"
    monkeypatch.setattr(gh_state_mirror, "GH_STATE_DIR", state_dir)
    monkeypatch.setattr(gh_state_mirror, "LOG_FILE", log_file)
    return state_dir


# ─── write_deferred ───────────────────────────────────────────────────


def test_write_deferred_produces_correct_frontmatter(gh_state_dir: Path) -> None:
    path = gh_state_mirror.write_deferred(
        "jcronq/alice",
        247,
        reason="router module not on master yet",
        deferred_by="speaking",
        title="Router cost tracking is broken",
    )
    assert path.exists()
    text = path.read_text()
    # Frontmatter shape — every documented field present.
    assert text.startswith("---\n")
    assert "type: deferred\n" in text
    assert "issue_number: 247\n" in text
    assert "repo: jcronq/alice\n" in text
    assert 'reason: "router module not on master yet"\n' in text
    assert "deferred_by: speaking\n" in text
    assert "deferred_at:" in text
    assert "updated_at:" in text
    assert "slug: gh-state-jcronq/alice-247\n" in text
    assert "tags: [gh-state]\n" in text
    assert "note_type: gh-state\n" in text
    # Heading + body.
    assert "# jcronq/alice#247 — Router cost tracking is broken\n" in text
    assert "Deferred. router module not on master yet.\n" in text


def test_write_deferred_without_title_omits_title_suffix(gh_state_dir: Path) -> None:
    path = gh_state_mirror.write_deferred(
        "jcronq/alice",
        99,
        reason="needs design",
        deferred_by="thinking",
    )
    text = path.read_text()
    assert "title: jcronq/alice#99\n" in text
    assert "# jcronq/alice#99\n" in text


def test_write_deferred_escapes_quotes_in_reason(gh_state_dir: Path) -> None:
    path = gh_state_mirror.write_deferred(
        "jcronq/alice",
        100,
        reason='waiting on "router" rebase',
        deferred_by="speaking",
    )
    text = path.read_text()
    # YAML stays parseable — the embedded quotes are escaped, not raw.
    assert 'reason: "waiting on \\"router\\" rebase"\n' in text


def test_write_deferred_is_idempotent_overwrite(gh_state_dir: Path) -> None:
    """Writing twice updates the file in place — no orphan tempfiles left."""
    gh_state_mirror.write_deferred("jcronq/alice", 247, "first reason", "speaking")
    gh_state_mirror.write_deferred("jcronq/alice", 247, "second reason", "thinking")
    note = gh_state_dir / "jcronq/alice-247.md"
    assert note.exists()
    text = note.read_text()
    assert 'reason: "second reason"\n' in text
    assert "deferred_by: thinking\n" in text
    # No tempfile detritus from the atomic-write rename.
    leftovers = [p for p in gh_state_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ─── read_state / is_deferred ─────────────────────────────────────────


def test_read_state_returns_none_when_missing(gh_state_dir: Path) -> None:
    assert gh_state_mirror.read_state("jcronq/alice", 999) is None
    assert gh_state_mirror.is_deferred("jcronq/alice", 999) is False


def test_read_state_parses_deferred_frontmatter(gh_state_dir: Path) -> None:
    gh_state_mirror.write_deferred(
        "jcronq/alice",
        247,
        reason="router module not on master",
        deferred_by="speaking",
        title="Router cost tracking",
    )
    state = gh_state_mirror.read_state("jcronq/alice", 247)
    assert state is not None
    assert state["type"] == "deferred"
    assert state["reason"] == "router module not on master"
    assert state["deferred_by"] == "speaking"
    assert state["issue_number"] == "247"
    assert gh_state_mirror.is_deferred("jcronq/alice", 247) is True


def test_read_state_parses_existing_issue_note(gh_state_dir: Path) -> None:
    gh_state_mirror.write_note_atomic(
        "jcronq/alice",
        300,
        {
            "_type": "issue",
            "state": "open",
            "title": "demo issue",
            "createdAt": "2026-05-19T00:00:00Z",
            "updatedAt": "2026-05-19T00:00:00Z",
        },
    )
    state = gh_state_mirror.read_state("jcronq/alice", 300)
    assert state is not None
    assert state["type"] == "issue"
    assert state["state"] == "open"
    assert gh_state_mirror.is_deferred("jcronq/alice", 300) is False


# ─── cleanup loop ─────────────────────────────────────────────────────


def _fake_gh_list_empty(*args: str) -> str:
    """gh stub that returns an empty JSON list for both issue and pr list."""
    return "[]"


def test_cleanup_preserves_deferred_when_no_open_issues(
    gh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even though no issue is open on the GitHub side, the deferred note
    must survive a cleanup pass. Before the fix, the cleanup loop's
    `type: issue` / `type: pr` filter would silently skip it — but the
    explicit guard guarantees future refactors don't accidentally enable
    a delete path.
    """
    # Seed a deferred note for an issue that GitHub will report as "not open".
    gh_state_mirror.write_deferred(
        "jcronq/alice",
        247,
        reason="router module not on master",
        deferred_by="speaking",
    )
    note = gh_state_dir / "jcronq/alice-247.md"
    assert note.exists()

    # Also seed a stale `type: issue` note for the same repo — it should be deleted.
    stale = gh_state_dir / "jcronq/alice-9999.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        "---\n"
        "slug: gh-state-jcronq/alice-9999\n"
        "type: issue\n"
        "state: open\n"
        "---\n\n"
        "# stale issue\n"
    )

    # gh CLI returns empty lists → all live notes look "closed".
    # Restrict REPOS so we don't try to call the real gh for cozyhem-engine.
    monkeypatch.setattr(gh_state_mirror, "REPOS", ["jcronq/alice"])
    monkeypatch.setattr(gh_state_mirror, "gh", _fake_gh_list_empty)

    gh_state_mirror.main()

    assert note.exists(), "deferred note must survive cleanup"
    assert not stale.exists(), "stale type:issue note must be removed"
    # Re-read content to confirm we didn't accidentally rewrite it.
    text = note.read_text()
    assert "type: deferred\n" in text


def test_cleanup_skips_deferred_explicitly(
    gh_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new guard — `if "type: deferred" in content: continue` —
    fires before the `type: issue` / `type: pr` branches. Confirm the
    guard short-circuits even when an attacker forges both markers
    in the same file (defense-in-depth)."""
    forged = gh_state_dir / "jcronq/alice-555.md"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(
        "---\n"
        "slug: gh-state-jcronq/alice-555\n"
        "type: deferred\n"
        "type: issue\n"          # forged second key — guard should still win
        "state: open\n"
        "---\n\n"
        "# forged\n"
    )

    monkeypatch.setattr(gh_state_mirror, "REPOS", ["jcronq/alice"])
    monkeypatch.setattr(gh_state_mirror, "gh", _fake_gh_list_empty)

    gh_state_mirror.main()
    assert forged.exists()


# ─── dispatcher read-path integration ─────────────────────────────────


def test_dispatcher_read_path_skip_when_deferred(gh_state_dir: Path) -> None:
    """Models the thinking-side dispatcher's pre-surface check:
    before writing a dispatch surface for `<repo>#<N>`, call
    ``is_deferred(repo, N)``. Iff False, write the surface.
    """
    repo, number = "jcronq/alice", 247
    # No state yet → dispatcher would proceed.
    assert gh_state_mirror.is_deferred(repo, number) is False

    # Speaking defers the issue.
    gh_state_mirror.write_deferred(repo, number, "blocked on router", "speaking")

    # Next dispatcher cycle hits the gate and skips.
    assert gh_state_mirror.is_deferred(repo, number) is True


def test_dispatcher_proceeds_when_only_open_issue_state(gh_state_dir: Path) -> None:
    """An open `type: issue` note (mirror-written) must not be treated as
    deferred — the dispatcher should fall through to writing a surface."""
    gh_state_mirror.write_note_atomic(
        "jcronq/alice",
        300,
        {
            "_type": "issue",
            "state": "open",
            "title": "fresh",
            "createdAt": "2026-05-19T00:00:00Z",
            "updatedAt": "2026-05-19T00:00:00Z",
        },
    )
    assert gh_state_mirror.is_deferred("jcronq/alice", 300) is False
