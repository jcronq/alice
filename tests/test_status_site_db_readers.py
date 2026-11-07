"""Tests for :mod:`status_site.sources.db_readers` (issue #790 Step 3).

The DB readers back the Overview when ``STATUS_SITE_READ_FROM=db``
(default). These tests seed a scratch ``alice-state.db`` directly via
the helper upserts and then exercise each reader for shape + filter
correctness.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from alice_state_db.schema import apply_schema
from status_site.sources import db_readers as R
from status_site.sources import done_today as D
from status_site.sources import pipeline as P
from status_site.sources import review_queue as RQ


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Return a path to a seeded alice-state.db with issues + PRs."""
    db_path = tmp_path / "alice-state.db"
    # Point the env override at this DB so the sources' default
    # resolution picks it up.
    monkeypatch.setenv("ALICE_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("STATUS_SITE_READ_FROM", "db")

    conn = sqlite3.connect(str(db_path))
    apply_schema(conn)

    # ── items ────────────────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO items (item_id, item_type, title, created_at, updated_at, author)
        VALUES
            ('gh:jcronq/alice#790', 'gh:issue', 'SM v2 → DB',
             '2026-11-05T10:00:00Z', '2026-11-07T10:00:00Z', 'jcronq'),
            ('gh:jcronq/alice#802', 'gh:issue', 'Seed design',
             '2026-11-06T00:00:00Z', '2026-11-06T10:00:00Z', 'jcronq'),
            ('gh:jcronq/alice#700', 'gh:issue', 'Unrelated other repo',
             '2026-11-01T00:00:00Z', '2026-11-01T10:00:00Z', 'jcronq'),
            ('gh:someone-else/repo#1', 'gh:issue', 'Not us',
             '2026-11-01T00:00:00Z', '2026-11-01T10:00:00Z', 'nobody'),
            ('gh:pr:jcronq/alice#802', 'gh:pr', 'feat: sm seed',
             '2026-11-06T09:00:00Z', '2026-11-06T18:00:00Z', 'jcronq'),
            ('gh:pr:jcronq/alice#803', 'gh:pr', 'feat: poller',
             '2026-11-07T09:00:00Z', '2026-11-07T10:00:00Z', 'jcronq')
        """
    )
    # ── state_machine ───────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO state_machine (item_id, state, entered_at)
        VALUES
            ('gh:jcronq/alice#790', 'selected', '2026-11-07T10:00:00Z'),
            ('gh:jcronq/alice#802', 'design_review', '2026-11-06T10:00:00Z'),
            ('gh:jcronq/alice#700', 'sm:selected', '2026-11-01T10:00:00Z'),
            ('gh:someone-else/repo#1', 'selected', '2026-11-01T10:00:00Z'),
            ('gh:pr:jcronq/alice#802', 'pr:merged', '2026-11-06T18:30:00Z'),
            ('gh:pr:jcronq/alice#803', 'pr:open', '2026-11-07T10:00:00Z')
        """
    )
    # Merged-at amendment for the merged PR so done_today can read it.
    # Use 15:00 UTC so the local-TZ bucketing lands inside "today"
    # regardless of the test host's zone (east of UTC-14 to west of
    # UTC+14 — spans every real zone comfortably).
    conn.execute(
        """
        INSERT INTO amendments (item_id, body, ts, source, source_ref)
        VALUES
            ('gh:pr:jcronq/alice#802', '2026-11-07T15:00:00Z',
             '2026-11-07T15:00:00Z', 'pr-merged-at',
             'gh:pr:jcronq/alice#802@2026-11-07T15:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ─── read_from ─────────────────────────────────────────────────────


def test_read_from_default_is_db(monkeypatch):
    monkeypatch.delenv(R.ENV_FLAG, raising=False)
    assert R.read_from() == R.MODE_DB


def test_read_from_respects_env(monkeypatch):
    monkeypatch.setenv(R.ENV_FLAG, "gh")
    assert R.read_from() == R.MODE_GH


def test_read_from_treats_garbage_as_default(monkeypatch):
    monkeypatch.setenv(R.ENV_FLAG, "nonsense")
    assert R.read_from() == R.MODE_DB


# ─── fetch_issues_from_db ─────────────────────────────────────────


def test_fetch_issues_from_db_returns_pipeline_shape(seeded_db):
    rows, failed = R.fetch_issues_from_db(["jcronq/alice"])
    assert failed == []
    # Two active issues on the repo (#790, #802) + #700 with legacy
    # "sm:" prefix that should also be normalised to selected.
    states = [(r["repo"], r["number"], r["state"]) for r in rows]
    assert ("jcronq/alice", 790, "selected") in states
    assert ("jcronq/alice", 802, "design_review") in states
    assert ("jcronq/alice", 700, "selected") in states
    for row in rows:
        assert row["repo"] == "jcronq/alice"
        assert row["url"].startswith("https://github.com/jcronq/alice/issues/")
        assert row["art"] == ""
        assert "updated_epoch" in row
        assert "body" in row


def test_fetch_issues_from_db_filters_by_repo(seeded_db):
    rows, _ = R.fetch_issues_from_db(["someone-else/repo"])
    assert len(rows) == 1
    assert rows[0]["repo"] == "someone-else/repo"


def test_fetch_issues_from_db_sorts_by_pipeline_rank(seeded_db):
    rows, _ = R.fetch_issues_from_db(["jcronq/alice"])
    ranks = [R._STATE_RANK[r["state"]] for r in rows]
    assert ranks == sorted(ranks)


def test_fetch_issues_from_db_missing_file_degrades_gracefully(tmp_path):
    # No DB file at all.
    rows, failed = R.fetch_issues_from_db(
        ["jcronq/alice"], db_path=tmp_path / "missing.db"
    )
    assert rows == [] and failed == []


# ─── fetch_design_review_from_db ──────────────────────────────────


def test_fetch_design_review_from_db(seeded_db):
    items, failed = R.fetch_design_review_from_db(
        ["jcronq/alice"], now=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    )
    assert failed == []
    assert len(items) == 1
    assert items[0].repo == "jcronq/alice"
    assert items[0].number == 802
    assert items[0].title == "Seed design"
    assert items[0].age  # non-empty age string


def test_fetch_design_review_from_db_filters_by_repo(seeded_db):
    items, _ = R.fetch_design_review_from_db(["someone-else/repo"])
    assert items == []


# ─── fetch_pr_review_from_db ──────────────────────────────────────


def test_fetch_pr_review_from_db(seeded_db):
    items, failed = R.fetch_pr_review_from_db(
        ["jcronq/alice"], now=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    )
    assert failed == []
    # Only the pr:open row surfaces; pr:merged is excluded.
    assert len(items) == 1
    assert items[0].number == 803
    assert items[0].url.endswith("/pull/803")


# ─── fetch_merged_prs_from_db ─────────────────────────────────────


def test_fetch_merged_prs_from_db_filters_to_today(seeded_db):
    # seeded_db put the merged_at at 2026-11-07T04:30:00Z;
    # that's within 2026-11-07 UTC and also within local
    # for most timezones. Pin ``today`` explicitly to avoid TZ drift.
    items, failed = R.fetch_merged_prs_from_db(
        ["jcronq/alice"],
        today=date(2026, 11, 7),
    )
    assert failed == []
    assert len(items) == 1
    assert items[0].number == 802


def test_fetch_merged_prs_from_db_excludes_other_days(seeded_db):
    items, _ = R.fetch_merged_prs_from_db(
        ["jcronq/alice"], today=date(2026, 11, 1)
    )
    assert items == []


# ─── Source-module wiring: pipeline / done_today / review_queue ────


def test_pipeline_fetch_issues_uses_db_by_default(seeded_db):
    # No runner= kwarg; env flag is 'db' (via seeded_db fixture).
    rows, failed = P.fetch_issues(["jcronq/alice"])
    assert failed == []
    assert any(r["number"] == 790 for r in rows)


def test_pipeline_fetch_issues_falls_back_to_gh_when_flag_is_gh(
    seeded_db, monkeypatch
):
    """``STATUS_SITE_READ_FROM=gh`` + no injected runner falls back to
    the real ``gh`` subprocess call. We intercept that call to
    confirm the gh path was taken rather than the DB path."""
    monkeypatch.setenv("STATUS_SITE_READ_FROM", "gh")
    import subprocess as _sp

    captured = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(_sp, "run", fake_run)
    rows, failed = P.fetch_issues(["jcronq/alice"])
    # The gh subprocess path should have fired at least once.
    assert any(
        isinstance(c, list) and len(c) >= 2 and c[0] == "gh" for c in captured
    ), captured
    # With the fake returning [], rows should be empty.
    assert rows == []


def test_done_today_fetch_merged_prs_uses_db_by_default(seeded_db):
    items, failed = D.fetch_merged_prs(
        ["jcronq/alice"], today=date(2026, 11, 7)
    )
    assert failed == []
    assert len(items) == 1
    assert items[0].number == 802


def test_review_queue_fetch_uses_db_by_default(seeded_db):
    design, prs, d_failed, p_failed = RQ.fetch(
        cfg_loader=lambda: {
            "repos_pr": ["jcronq/alice"],
            "repos_design_review": ["jcronq/alice"],
        }
    )
    assert d_failed == [] and p_failed == []
    # 1 design-review issue (#802), 1 open PR (#803).
    assert len(design) == 1 and design[0].number == 802
    assert len(prs) == 1 and prs[0].number == 803


def test_review_queue_fetch_respects_explicit_runner(seeded_db, monkeypatch):
    """Explicit ``runner=`` kwarg forces the subprocess path even when
    the env flag is set to db — ensures existing tests keep working.
    """
    monkeypatch.setenv("STATUS_SITE_READ_FROM", "db")
    import subprocess

    captured = []

    def runner(cmd):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    design, prs, _, _ = RQ.fetch(
        runner=runner,
        cfg_loader=lambda: {
            "repos_pr": ["jcronq/alice"],
            "repos_design_review": ["jcronq/alice"],
        },
    )
    assert captured, "runner override should have fired"
    # The runner returns empty, so the rows are empty — distinct from
    # the DB path which would have returned real seeded rows.
    assert design == [] and prs == []
