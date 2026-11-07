"""End-to-end integration: GH poller → DB → status-site reader.

Exercises the Step 2 + Step 3 flip from #790 together — the poller
writes real rows into a scratch DB, then the three status-site source
modules read them back through their env-flag-selected DB paths.

This catches regressions where the poller and reader drift (one side
changes a column, state string, or item_id shape and the other
doesn't). The end-to-end harness is the single artifact that pins
both sides to the same contract.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date

import pytest

from alice_state_db import gh_poll as GP
from status_site.sources import done_today as D
from status_site.sources import pipeline as P
from status_site.sources import review_queue as RQ


def _mk_curl_output(status: int, etag: str | None, body: str) -> str:
    header_lines = [f"HTTP/2 {status}"]
    if etag:
        header_lines.append(f'ETag: "{etag}"')
    header_lines.append("Content-Type: application/json")
    return "\r\n".join(header_lines) + "\r\n\r\n" + body


_ISSUES_RESPONSE = [
    {
        "number": 790,
        "title": "SM v2 → DB",
        "created_at": "2026-11-05T10:00:00Z",
        "updated_at": "2026-11-07T10:00:00Z",
        "user": {"login": "jcronq"},
        "body": "design body",
        "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
    },
    {
        "number": 802,
        "title": "Seed",
        "created_at": "2026-11-06T00:00:00Z",
        "updated_at": "2026-11-06T10:00:00Z",
        "user": {"login": "jcronq"},
        "body": "",
        "labels": [{"name": "sm:design_review"}, {"name": "art:code"}],
    },
]

_PRS_RESPONSE = [
    {
        "number": 803,
        "title": "feat: poller",
        "created_at": "2026-11-07T09:00:00Z",
        "updated_at": "2026-11-07T10:00:00Z",
        "user": {"login": "jcronq"},
        "body": "Closes #790",
        "state": "open",
        "merged_at": None,
        "draft": False,
    },
]


def _runner(cmd):
    url = cmd[-1]
    if "/issues" in url and "/pulls" not in url:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=_mk_curl_output(200, "issues-etag", json.dumps(_ISSUES_RESPONSE)),
            stderr="",
        )
    if "/pulls" in url:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=_mk_curl_output(200, "prs-etag", json.dumps(_PRS_RESPONSE)),
            stderr="",
        )
    return subprocess.CompletedProcess(
        cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
    )


@pytest.fixture
def seeded_from_poll(tmp_path, monkeypatch):
    """Run the poller once and return the DB path."""
    mind = tmp_path / "alice-mind"
    (mind / "inner" / "state").mkdir(parents=True)
    db_path = mind / "inner" / "state" / "alice-state.db"

    monkeypatch.setenv("ALICE_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("STATUS_SITE_READ_FROM", "db")
    monkeypatch.setenv(
        "ALICE_GH_RATE_LIMIT_BACKOFF_PATH",
        str(mind / "inner" / "state" / "gh-rate-limit-backoff.json"),
    )

    report = GP.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},
        runner=_runner,
    )
    assert report.status == "ok", report.errors
    return db_path


def test_poller_populates_db_pipeline_source_sees_rows(seeded_from_poll):
    """Pipeline source, reading from the poller-written DB, sees both
    issues with their correct SM states."""
    rows, failed = P.fetch_issues(["jcronq/alice"])
    assert failed == []
    states = {(r["number"], r["state"]) for r in rows}
    assert (790, "selected") in states
    assert (802, "design_review") in states


def test_poller_populates_db_review_queue_sees_design_and_pr(seeded_from_poll):
    """Review-queue source sees #802 as design-review + #803 as open-PR."""
    design, prs, _, _ = RQ.fetch(
        cfg_loader=lambda: {
            "repos_pr": ["jcronq/alice"],
            "repos_design_review": ["jcronq/alice"],
        }
    )
    assert [d.number for d in design] == [802]
    assert [p.number for p in prs] == [803]


def test_poller_populates_db_done_today_reports_zero_merged_prs(
    seeded_from_poll,
):
    """No merged PRs in the fake response → done-today is empty."""
    items, failed = D.fetch_merged_prs(["jcronq/alice"], today=date(2026, 11, 7))
    assert failed == []
    assert items == []


def test_fallback_to_gh_when_flag_flipped(seeded_from_poll, monkeypatch):
    """Flipping ``STATUS_SITE_READ_FROM`` back to ``gh`` sends traffic
    to ``gh`` subprocess instead of the DB.

    We intercept the subprocess call to prove the rollback path is wired.
    """
    monkeypatch.setenv("STATUS_SITE_READ_FROM", "gh")
    import subprocess as _sp

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd[0] if cmd else "")
        return _sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(_sp, "run", fake_run)
    rows, failed = P.fetch_issues(["jcronq/alice"])
    assert "gh" in calls, calls
    # Fake returned [], so the result is empty — distinct from the DB
    # reader's seeded answer.
    assert rows == []
