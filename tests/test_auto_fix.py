"""Tests for the Speaking-side auto-fix dispatch wiring.

Pins the dispatched-in-flight integration that closes the dispatcher
race window described in
``cortex-memory/research/2026-05-19-dispatched-inflight-speaking-wiring.md``:
when Speaking's ``_dispatch_subagent`` is about to spawn a worker for a
prompt that matches the auto-fix template,
:func:`alice_speaking.auto_fix.record_auto_fix_inflight` must call
:func:`alice_forge.gh_state_mirror.write_dispatched_inflight` BEFORE
the asyncio task starts.

The integration is exercised end-to-end via the parser + record helper.
We don't boot the full daemon — the unit under test is the thin Python
wrapper that owns the bookkeeping.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from alice_speaking import auto_fix


# Minimal verbatim template snippet — the leading line is what the
# parser keys off. If the template at
# cortex-memory/reference/auto-fix-worker-prompt.md ever changes its
# leading line, this fixture (and the regex in auto_fix.py) must move
# together.
_AUTO_FIX_PROMPT = (
    "You are an auto-fix worker for issue #261 in cronqj/cozyhem-engine "
    "from @cronqj.\n"
    "\n"
    "Issue body:\n"
    "\n"
    "(omitted for brevity)\n"
)


def test_parse_recognises_auto_fix_header() -> None:
    parsed = auto_fix.parse_auto_fix_dispatch(_AUTO_FIX_PROMPT)
    assert parsed == ("cronqj/cozyhem-engine", 261)


def test_parse_returns_none_for_unrelated_prompt() -> None:
    assert auto_fix.parse_auto_fix_dispatch("research the foo bar") is None
    assert auto_fix.parse_auto_fix_dispatch("") is None


def test_parse_handles_repo_with_dots_and_dashes() -> None:
    prompt = (
        "You are an auto-fix worker for issue #7 in my-org/cozy.hem-engine "
        "from @alice-bot.\n"
    )
    assert auto_fix.parse_auto_fix_dispatch(prompt) == (
        "my-org/cozy.hem-engine",
        7,
    )


def test_record_calls_write_dispatched_inflight_with_parsed_args(
    tmp_path: Path,
) -> None:
    expected_path = tmp_path / "gh-state" / "cronqj-cozyhem-engine-261.md"
    with patch.object(
        auto_fix.gh_state_mirror,
        "write_dispatched_inflight",
        return_value=expected_path,
    ) as mock_write:
        result = auto_fix.record_auto_fix_inflight(
            _AUTO_FIX_PROMPT, worker_id="bg-deadbeef0000"
        )

    assert result == expected_path
    mock_write.assert_called_once_with(
        "cronqj/cozyhem-engine", 261, "bg-deadbeef0000", title=""
    )


def test_record_returns_none_for_non_auto_fix_prompt() -> None:
    with patch.object(
        auto_fix.gh_state_mirror, "write_dispatched_inflight"
    ) as mock_write:
        result = auto_fix.record_auto_fix_inflight(
            "go research the cue runner", worker_id="bg-xyz"
        )
    assert result is None
    mock_write.assert_not_called()


def test_record_swallows_write_errors_so_dispatch_continues() -> None:
    """A bookkeeping FS error must not propagate — the worker dispatch
    is the priority; the 4-hour timeout cleanup will reap any stale
    record. The PR-open cron pass overwrites it on the success path."""
    with patch.object(
        auto_fix.gh_state_mirror,
        "write_dispatched_inflight",
        side_effect=OSError("disk full"),
    ) as mock_write:
        result = auto_fix.record_auto_fix_inflight(
            _AUTO_FIX_PROMPT, worker_id="bg-fffeeeddd"
        )
    assert result is None
    mock_write.assert_called_once()
