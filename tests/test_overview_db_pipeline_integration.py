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


# ─── issue #821: components propagate through the full pipeline ─────


def _mk_curl_output_shim(status, etag, body):
    return _mk_curl_output(status, etag, body)


def _integration_runner(cmd):
    """Extended runner that serves per-PR file lists for the component
    bucketing path (issue #821). Adds a file-list payload for PR #803."""
    import re as _re

    url = cmd[-1]
    base = url.split("?", 1)[0]
    m = _re.search(r"/pulls/(\d+)/files$", base)
    if m:
        number = int(m.group(1))
        files = (
            [
                {"filename": "src/status_site/sources/pipeline.py"},
                {"filename": "src/status_site/static/index.html"},
            ]
            if number == 803
            else []
        )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=_mk_curl_output(200, f'"files-{number}"', json.dumps(files)),
            stderr="",
        )
    # fall through to the existing runner
    return _runner(cmd)


@pytest.fixture
def seeded_with_components(tmp_path, monkeypatch):
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
        runner=_integration_runner,
    )
    assert report.status == "ok", report.errors
    return db_path


def test_components_flow_from_poller_to_group_by_component(
    seeded_with_components,
):
    """Poller writes components → db reader returns them → the renderer
    groups by component. This pins the wire shape across the boundary."""
    rows, _ = P.fetch_issues(["jcronq/alice"])
    # Issue #790 is linked from PR #803 (Closes #790) in the fixture
    # response, so it inherits status_site.
    row_790 = next(r for r in rows if r["number"] == 790)
    assert "status_site" in (row_790.get("components") or [])
    # Issue #802 has no PR closing it → pre-build.
    row_802 = next(r for r in rows if r["number"] == 802)
    assert (row_802.get("components") or []) == ["pre-build"]

    groups = P.group_rows_by_component(P.issue_rows(rows, now=1_800_000_000.0))
    comps = {g["component"] for g in groups}
    assert "status_site" in comps
    assert "pre-build" in comps
