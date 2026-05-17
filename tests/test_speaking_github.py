"""Tests for ``alice_speaking.github`` — the self-filed issue helper.

Pairs with ``test_gh_watcher.test_self_filed_marker_suppresses_new_issue_note``
to round-trip the marker through both sides. If either constant drifts,
one of these tests will fail.
"""

from __future__ import annotations

from typing import Any

from alice_speaking import github as speaking_gh
from alice_watchers import github as gh_watcher


def test_marker_matches_watcher_constant() -> None:
    """The watcher keys off the same string Speaking stamps. Drift on
    either side would silently re-enable the noise loop (issue #226)."""
    assert speaking_gh.SELF_FILED_MARKER == gh_watcher.SELF_FILED_MARKER


def test_stamp_self_filed_appends_when_missing() -> None:
    body = "Fix the thing.\n\nDetails here."
    stamped = speaking_gh.stamp_self_filed(body)
    assert stamped.endswith(speaking_gh.SELF_FILED_MARKER + "\n")
    # Original body is preserved verbatim above the marker.
    assert stamped.startswith("Fix the thing.\n\nDetails here.")


def test_stamp_self_filed_is_idempotent() -> None:
    """Templates that already include the marker shouldn't double-stamp."""
    body = f"Some content.\n\n{speaking_gh.SELF_FILED_MARKER}\n"
    assert speaking_gh.stamp_self_filed(body) == body


def test_stamp_self_filed_handles_empty_body() -> None:
    assert speaking_gh.stamp_self_filed("") == speaking_gh.SELF_FILED_MARKER + "\n"
    assert (
        speaking_gh.stamp_self_filed("   \n\n  ")
        == speaking_gh.SELF_FILED_MARKER + "\n"
    )


def test_create_issue_invokes_gh_and_stamps_marker(monkeypatch: Any) -> None:
    """``create_issue`` shells out to ``gh issue create`` with the body
    pre-stamped. The watcher round-trip is covered separately; here we
    just verify the args shape and that the marker reaches ``--body``."""
    captured: dict[str, Any] = {}

    class FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(args: list[str], **kwargs: Any) -> FakeCompleted:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeCompleted("https://github.com/acme/widgets/issues/42\n")

    monkeypatch.setattr(speaking_gh.subprocess, "run", fake_run)

    url = speaking_gh.create_issue(
        repo="acme/widgets",
        title="Pipe leaks",
        body="The pipe leaks.",
        labels=["bug", "triage"],
    )

    assert url == "https://github.com/acme/widgets/issues/42"
    args = captured["args"]
    assert args[:5] == ["gh", "issue", "create", "--repo", "acme/widgets"]
    assert "--title" in args and args[args.index("--title") + 1] == "Pipe leaks"
    body_index = args.index("--body") + 1
    body_arg = args[body_index]
    assert "The pipe leaks." in body_arg
    assert speaking_gh.SELF_FILED_MARKER in body_arg
    # Labels are forwarded one --label per value.
    label_args = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    assert label_args == ["bug", "triage"]
    assert captured["kwargs"]["check"] is True
    assert captured["kwargs"]["capture_output"] is True


def test_create_issue_does_not_double_stamp(monkeypatch: Any) -> None:
    """A body that already carries the marker — e.g. composed from a
    template — passes through with exactly one marker copy."""
    captured: dict[str, Any] = {}

    class FakeCompleted:
        stdout = "https://example.com/x\n"
        stderr = ""
        returncode = 0

    def fake_run(args: list[str], **kwargs: Any) -> FakeCompleted:
        captured["args"] = args
        return FakeCompleted()

    monkeypatch.setattr(speaking_gh.subprocess, "run", fake_run)

    pre_stamped = f"already done\n\n{speaking_gh.SELF_FILED_MARKER}\n"
    speaking_gh.create_issue(repo="acme/widgets", title="t", body=pre_stamped)

    body_arg = captured["args"][captured["args"].index("--body") + 1]
    assert body_arg.count(speaking_gh.SELF_FILED_MARKER) == 1
