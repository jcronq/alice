"""Unit tests for :mod:`alice_speaking.worker_report`.

Covers the three public contracts:

* :func:`wrap_instructions` — idempotent preamble prepend.
* :func:`parse_worker_report` — trailing-``json`` extraction.
* :func:`validate_worker_report` — required-field matrix keyed on
  ``kind``.

Plus the :func:`looks_like_done_without_evidence` heuristic (shipped
in this module for the Stop-evidence overlay) and the
:func:`write_pending_verify_note` filesystem helper.
"""

from __future__ import annotations

import json
from pathlib import Path


from alice_speaking import worker_report


# ---------------------------------------------------------------------------
# wrap_instructions


def test_wrap_instructions_prepends_preamble() -> None:
    text = "do the thing"
    wrapped = worker_report.wrap_instructions(text)
    assert wrapped.startswith("# Worker protocol")
    assert text in wrapped
    assert "REQUIRED-REPORT" in wrapped


def test_wrap_instructions_is_idempotent() -> None:
    once = worker_report.wrap_instructions("do the thing")
    twice = worker_report.wrap_instructions(once)
    assert once == twice


def test_wrap_instructions_handles_empty_input() -> None:
    wrapped = worker_report.wrap_instructions("")
    assert "REQUIRED-REPORT" in wrapped


# ---------------------------------------------------------------------------
# parse_worker_report


def _report_text(payload: dict) -> str:
    return (
        "Here is the summary of what I did.\n\n"
        "```json\n" + json.dumps(payload, indent=2) + "\n```"
    )


def test_parse_worker_report_extracts_trailing_block() -> None:
    payload = {
        "kind": "code",
        "branch": "feat/x",
        "commit_sha": "a" * 40,
        "pr_url": "https://github.com/jcronq/alice/pull/999",
        "baseline_suite": {"passed": 10, "failed": 0, "errors": 0, "skipped": 1},
        "full_suite_after": {
            "passed": 11,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
        },
        "live_verified": "n/a",
        "amendments_addressed": [],
        "unresolved": [],
        "lessons": [],
    }
    parsed = worker_report.parse_worker_report(_report_text(payload))
    assert parsed == payload


def test_parse_worker_report_prefers_trailing_block_over_earlier_fences() -> None:
    # A worker that quoted a JSON sample mid-report must still have
    # its ACTUAL report at the end.
    earlier = {"kind": "code", "example": "quoting the issue body"}
    actual = {
        "kind": "code",
        "branch": "feat/x",
        "commit_sha": "b" * 40,
        "full_suite_after": {
            "passed": 1,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        },
    }
    text = (
        "Here is some context.\n\n"
        "```json\n" + json.dumps(earlier) + "\n```\n\n"
        "And here is my actual report.\n\n"
        "```json\n" + json.dumps(actual) + "\n```"
    )
    parsed = worker_report.parse_worker_report(text)
    assert parsed is not None
    assert parsed.get("commit_sha") == "b" * 40


def test_parse_worker_report_returns_none_when_block_missing() -> None:
    assert worker_report.parse_worker_report("just prose, no fence") is None


def test_parse_worker_report_returns_none_on_invalid_json() -> None:
    bad = "trailing fence but junk\n\n```json\n{not: json}\n```"
    assert worker_report.parse_worker_report(bad) is None


def test_parse_worker_report_handles_empty_string() -> None:
    assert worker_report.parse_worker_report("") is None


def test_parse_worker_report_tolerates_trailing_whitespace() -> None:
    payload = {"kind": "research", "live_verified": "cortex-memory/foo.md"}
    text = _report_text(payload) + "\n\n   \n"
    parsed = worker_report.parse_worker_report(text)
    assert parsed == payload


# ---------------------------------------------------------------------------
# validate_worker_report


def _ok_code_report() -> dict:
    return {
        "kind": "code",
        "branch": "feat/x",
        "commit_sha": "a" * 40,
        "pr_url": "https://github.com/jcronq/alice/pull/999",
        "baseline_suite": {
            "passed": 10,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
        },
        "full_suite_after": {
            "passed": 11,
            "failed": 0,
            "errors": 0,
            "skipped": 1,
        },
        "live_verified": "n/a",
        "amendments_addressed": [],
        "unresolved": [],
        "lessons": [],
    }


def test_validate_worker_report_accepts_well_formed_code_report() -> None:
    assert worker_report.validate_worker_report(_ok_code_report()) == []


def test_validate_worker_report_rejects_code_without_commit_sha() -> None:
    report = _ok_code_report()
    report["commit_sha"] = ""
    issues = worker_report.validate_worker_report(report)
    assert any("commit_sha" in issue for issue in issues)


def test_validate_worker_report_rejects_code_without_full_suite_after() -> None:
    report = _ok_code_report()
    del report["full_suite_after"]
    issues = worker_report.validate_worker_report(report)
    assert any("full_suite_after" in issue for issue in issues)


def test_validate_worker_report_accepts_research_without_commit_sha() -> None:
    report = {
        "kind": "research",
        "live_verified": "cortex-memory/research/2026-11-02-note.md",
    }
    assert worker_report.validate_worker_report(report) == []


def test_validate_worker_report_rejects_research_without_live_verified() -> None:
    report = {"kind": "research", "live_verified": ""}
    issues = worker_report.validate_worker_report(report)
    assert any("live_verified" in issue for issue in issues)


def test_validate_worker_report_flags_bad_suite_shape() -> None:
    report = _ok_code_report()
    report["full_suite_after"] = {"passed": "ten", "failed": 0}
    issues = worker_report.validate_worker_report(report)
    assert any("passed" in issue and "integer" in issue for issue in issues)
    assert any("errors" in issue and "missing" in issue for issue in issues)


def test_validate_worker_report_flags_unknown_kind() -> None:
    report = {"kind": "whatever"}
    issues = worker_report.validate_worker_report(report)
    assert any("kind" in issue for issue in issues)


def test_validate_worker_report_allows_null_baseline_for_tactical_diff() -> None:
    # The tactical carve-out: baseline_suite is permitted to be null.
    report = _ok_code_report()
    report["baseline_suite"] = None
    assert worker_report.validate_worker_report(report) == []


# ---------------------------------------------------------------------------
# looks_like_done_without_evidence (Stop-evidence overlay)


def test_claim_done_without_evidence_fires() -> None:
    text = "Done! I fixed the thing and all is well."
    assert worker_report.looks_like_done_without_evidence(text) is True


def test_claim_done_with_commit_sha_passes() -> None:
    text = "Done. See commit 0123456789ab for the change."
    assert worker_report.looks_like_done_without_evidence(text) is False


def test_claim_done_with_pr_number_passes() -> None:
    text = "Fixed in PR #777."
    assert worker_report.looks_like_done_without_evidence(text) is False


def test_claim_done_with_test_counts_passes() -> None:
    text = "Done; tests show 123 passing green."
    assert worker_report.looks_like_done_without_evidence(text) is False


def test_text_without_done_claim_does_not_fire() -> None:
    text = "I investigated and here's what I found."
    assert worker_report.looks_like_done_without_evidence(text) is False


def test_full_suite_after_mention_counts_as_evidence() -> None:
    text = "Merged and complete. full_suite_after captured."
    assert worker_report.looks_like_done_without_evidence(text) is False


# ---------------------------------------------------------------------------
# write_pending_verify_note


def test_write_pending_verify_note_lands_in_inner_notes(tmp_path: Path) -> None:
    path = worker_report.write_pending_verify_note(
        tmp_path,
        handle="bg-abc",
        description="dispatched task",
        reasons=["commit_sha missing", "no trailing JSON block"],
        result_excerpt="worker said: all done!",
    )
    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path / "inner" / "notes"
    body = path.read_text()
    assert "pending-verify" in body
    assert "bg-abc" in body
    assert "commit_sha missing" in body
    assert "all done!" in body


def test_write_pending_verify_note_truncates_long_excerpt(tmp_path: Path) -> None:
    long_excerpt = "x" * 5000
    path = worker_report.write_pending_verify_note(
        tmp_path,
        handle="bg-xyz",
        description="dispatched task",
        reasons=["r"],
        result_excerpt=long_excerpt,
    )
    assert path is not None
    body = path.read_text()
    # Guard: the excerpt should be clipped to roughly 1500 chars so
    # the note stays scannable.
    assert body.count("x") <= 1500


def test_write_pending_verify_note_returns_none_on_bad_mind_dir() -> None:
    out = worker_report.write_pending_verify_note(
        None,  # type: ignore[arg-type]
        handle="bg-abc",
        description="x",
        reasons=["r"],
        result_excerpt="",
    )
    assert out is None
