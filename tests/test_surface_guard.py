"""Tests for ``alice_thinking.surface_guard``.

Covers the acceptance criteria from
``cortex-memory/research/2026-05-18-surface-guard-design.md``:

1. Hallucinated path → blocked (downgrade to insight).
2. Existing sandbox file → passes.
3. Path outside sandbox → blocked with reason.
4. ``gh_issue_state`` mocked, never hits live GitHub.
5. Regression: 2026-05-18-141600-missed-reply-gh-push shape returns False.
6. Insight-tier passes through.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alice_thinking import surface_guard
from alice_thinking.surface_guard import should_fire


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """Point SANDBOX at a tmp dir so tests can stage real files."""
    fake_sandbox = tmp_path / "alice-mind"
    fake_sandbox.mkdir()
    monkeypatch.setattr(surface_guard, "SANDBOX", fake_sandbox)
    surface_guard._reset_cache()
    yield fake_sandbox


# ---------------------------------------------------------------------------
# file_exists checks
# ---------------------------------------------------------------------------


def test_hallucinated_path_caught():
    """Flash surface claiming a non-existent path must be blocked."""
    can_fire, reason = should_fire(
        target="check-new-config",
        evidence={"config_file": "alice-speaking/config.yaml"},
        priority="flash",
    )
    assert can_fire is False
    assert "does not exist" in reason
    assert "config_file" in reason


def test_existing_sandbox_file_passes(_sandbox):
    """A file that actually exists in the sandbox should pass."""
    design = _sandbox / "cortex-memory" / "research"
    design.mkdir(parents=True)
    (design / "2026-05-18-surface-guard-design.md").write_text("# design")

    can_fire, reason = should_fire(
        target="check-design",
        evidence={
            "design": "cortex-memory/research/2026-05-18-surface-guard-design.md"
        },
        priority="flash",
    )
    assert can_fire is True
    assert reason == "all claims verifiable"


def test_absolute_path_outside_sandbox_blocked():
    """``/home/alice/foo`` is outside sandbox — unverifiable by Thinking."""
    can_fire, reason = should_fire(
        target="check-host-file",
        evidence={"host_file": "/home/alice/foo"},
        priority="flash",
    )
    assert can_fire is False
    assert "outside sandbox" in reason


def test_absolute_path_inside_sandbox_resolves(_sandbox):
    """An absolute path that lives under SANDBOX is allowed if it exists."""
    target = _sandbox / "ok.md"
    target.write_text("ok")
    can_fire, _ = should_fire(
        target="t",
        evidence={"f": str(target)},
        priority="flash",
    )
    assert can_fire is True


# ---------------------------------------------------------------------------
# Insight pass-through
# ---------------------------------------------------------------------------


def test_insight_passes_through():
    """Non-flash surfaces skip the guard entirely."""
    can_fire, reason = should_fire(
        target="anything",
        evidence={"nonexistent": "/no/such/path"},
        priority="insight",
    )
    assert can_fire is True
    assert "no guard needed" in reason


def test_unknown_priority_passes_through():
    """Anything that isn't literally 'flash' is unguarded."""
    can_fire, _ = should_fire(
        target="t", evidence={"x": "/nope"}, priority="routine"
    )
    assert can_fire is True


# ---------------------------------------------------------------------------
# Empty / malformed evidence
# ---------------------------------------------------------------------------


def test_flash_with_no_evidence_blocked():
    """Flash + empty evidence cannot be verified — fail closed."""
    can_fire, reason = should_fire(target="bare", evidence={}, priority="flash")
    assert can_fire is False
    assert "no evidence" in reason


def test_unknown_check_type_blocked():
    can_fire, reason = should_fire(
        target="t",
        evidence=[{"type": "telepathy", "claim": "trust me"}],
        priority="flash",
    )
    assert can_fire is False
    assert "unknown check type" in reason


# ---------------------------------------------------------------------------
# gh_issue_state — mocked, never hits live GitHub
# ---------------------------------------------------------------------------


def _fake_gh_run(stdout: str, returncode: int = 0):
    class _R:
        pass

    r = _R()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


def test_gh_issue_state_all_present():
    fake_stdout = '[{"number":240,"state":"OPEN"},{"number":242,"state":"CLOSED"}]'
    with patch.object(
        surface_guard.subprocess,
        "run",
        return_value=_fake_gh_run(fake_stdout),
    ) as m:
        can_fire, reason = should_fire(
            target="file-issue",
            evidence=[
                {
                    "type": "gh_issue_state",
                    "repo": "jcronq/alice",
                    "numbers": [240, 242],
                }
            ],
            priority="flash",
        )
    assert can_fire is True
    assert m.call_count == 1


def test_gh_issue_state_missing_blocked():
    fake_stdout = '[{"number":240,"state":"OPEN"}]'
    with patch.object(
        surface_guard.subprocess,
        "run",
        return_value=_fake_gh_run(fake_stdout),
    ):
        can_fire, reason = should_fire(
            target="file-issue",
            evidence=[
                {
                    "type": "gh_issue_state",
                    "repo": "jcronq/alice",
                    "numbers": [999],
                }
            ],
            priority="flash",
        )
    assert can_fire is False
    assert "999" in reason


def test_gh_issue_state_unavailable_fails_closed():
    with patch.object(
        surface_guard.subprocess,
        "run",
        side_effect=OSError("network down"),
    ):
        can_fire, reason = should_fire(
            target="file-issue",
            evidence=[
                {
                    "type": "gh_issue_state",
                    "repo": "jcronq/alice",
                    "numbers": [1],
                }
            ],
            priority="flash",
        )
    assert can_fire is False
    assert "github unavailable" in reason


def test_gh_issue_state_memoized_per_wake():
    """Two checks against the same repo hit gh once."""
    fake_stdout = '[{"number":1,"state":"OPEN"}]'
    with patch.object(
        surface_guard.subprocess,
        "run",
        return_value=_fake_gh_run(fake_stdout),
    ) as m:
        should_fire(
            target="t",
            evidence=[
                {"type": "gh_issue_state", "repo": "jcronq/alice", "numbers": [1]}
            ],
            priority="flash",
        )
        should_fire(
            target="t",
            evidence=[
                {"type": "gh_issue_state", "repo": "jcronq/alice", "numbers": [1]}
            ],
            priority="flash",
        )
    assert m.call_count == 1


# ---------------------------------------------------------------------------
# Regression — 2026-05-18 missed-reply-gh-push false alarm
# ---------------------------------------------------------------------------


def test_regression_2026_05_18_missed_reply_gh_push():
    """The flash surface that triggered the original design.

    Thinking claimed ``alice_speaking/_dispatch.py`` lines 140-157 carried
    a missed-reply fix that hadn't been pushed, citing ``/home/alice`` as
    not-a-git-repo. Both the source file and the host dir are outside her
    sandbox — the claim is unverifiable. Guard must block this shape.
    """
    evidence = [
        {
            "type": "file_exists",
            "path": "alice_speaking/_dispatch.py",
            "claim": "missed-reply-fix-location",
        },
        {
            "type": "file_exists",
            "path": "/home/alice",
            "claim": "host-git-repo",
        },
    ]
    can_fire, reason = should_fire(
        target="missed-reply-gh-push",
        evidence=evidence,
        priority="flash",
    )
    assert can_fire is False
    # First-failing check is the relative path; either reason text is acceptable.
    assert (
        "missed-reply-fix-location" in reason
        or "host-git-repo" in reason
        or "outside sandbox" in reason
        or "does not exist" in reason
    )


# ---------------------------------------------------------------------------
# daily_contains
# ---------------------------------------------------------------------------


def test_daily_contains_keyword_present(_sandbox):
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    daily_dir = _sandbox / "cortex-memory" / "dailies"
    daily_dir.mkdir(parents=True)
    (daily_dir / f"{today}.md").write_text("Logged: deploy-event")

    can_fire, _ = should_fire(
        target="t",
        evidence=[{"type": "daily_contains", "keyword": "deploy-event"}],
        priority="flash",
    )
    assert can_fire is True


def test_daily_contains_keyword_missing_blocked(_sandbox):
    can_fire, reason = should_fire(
        target="t",
        evidence=[{"type": "daily_contains", "keyword": "never-written"}],
        priority="flash",
    )
    assert can_fire is False
    assert "daily_contains" in reason
