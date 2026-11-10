"""Tests for :mod:`alice_state_db.gh_poll` (issue #790 Step 2)."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

from alice_forge import rate_limit as _rate_limit_mod
from alice_state_db import gh_poll as P


# ─── Curl-wrapper parsers ────────────────────────────────────────────


def _mk_curl_output(status: int, etag: str | None, body: str, extra_headers: str = "") -> str:
    """Stitch together a header + body response the way ``curl -D -`` does."""
    header_lines = [f"HTTP/2 {status}"]
    if etag:
        header_lines.append(f'ETag: "{etag}"')
    if extra_headers:
        header_lines.append(extra_headers)
    header_lines.append("Content-Type: application/json")
    return "\r\n".join(header_lines) + "\r\n\r\n" + body


def test_parse_curl_output_extracts_status_etag_and_body():
    stdout = _mk_curl_output(200, "abc123", '[{"n":1}]')
    result = P._parse_curl_output(stdout)
    assert result.status == 200
    assert result.etag == '"abc123"'
    assert result.body == '[{"n":1}]'


def test_parse_curl_output_304_has_no_body():
    stdout = _mk_curl_output(304, "xyz", "")
    result = P._parse_curl_output(stdout)
    assert result.status == 304
    assert result.body is None
    assert result.etag == '"xyz"'


def test_parse_curl_output_detects_rate_limit_remaining():
    stdout = _mk_curl_output(
        403, None, '{"message": "rate limit"}', "X-RateLimit-Remaining: 0"
    )
    result = P._parse_curl_output(stdout)
    assert result.status == 403
    assert result.rate_limit_remaining == 0


# ─── fetch_github ──────────────────────────────────────────────────────


def _runner_from_table(table: dict[str, tuple[int, str | None, str]]):
    """Build a fake subprocess runner keyed by URL."""

    def run(cmd):
        url = cmd[-1]
        status, etag, body = table[url]
        stdout = _mk_curl_output(status, etag, body)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run


def test_fetch_github_passes_if_none_match_header():
    captured = {}

    def run(cmd):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(304, "abc", ""), stderr=""
        )

    result = P.fetch_github(
        "https://api.github.com/foo", token="ghp_x", etag='"abc"', runner=run
    )
    # If-None-Match must be sent.
    assert any(
        arg == 'If-None-Match: "abc"' for arg in captured["cmd"]
    )
    # Authorization header carries the token.
    assert any("Authorization: Bearer ghp_x" in a for a in captured["cmd"])
    assert result.status == 304


def test_fetch_github_raises_on_429():
    def run(cmd):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(429, None, "slow down"), stderr=""
        )

    with pytest.raises(P.GHPollError) as exc_info:
        P.fetch_github("https://api.github.com/foo", token=None, runner=run)
    assert exc_info.value.looks_like_rate_limit is True


def test_fetch_github_raises_on_403_with_zero_remaining():
    def run(cmd):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=_mk_curl_output(
                403,
                None,
                '{"message":"API rate limit exceeded"}',
                "X-RateLimit-Remaining: 0",
            ),
            stderr="",
        )

    with pytest.raises(P.GHPollError) as exc_info:
        P.fetch_github("https://api.github.com/foo", token=None, runner=run)
    assert exc_info.value.looks_like_rate_limit is True


# ─── ETag cache ──────────────────────────────────────────────────────


def test_etag_cache_roundtrip(tmp_path):
    cache_path = tmp_path / "etags.json"
    P._write_etag_cache(cache_path, {"jcronq/alice:issues": '"abc"'})
    assert P._read_etag_cache(cache_path) == {"jcronq/alice:issues": '"abc"'}


def test_etag_cache_handles_missing_file(tmp_path):
    assert P._read_etag_cache(tmp_path / "missing.json") == {}


def test_etag_cache_handles_corrupt_file(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("not json")
    assert P._read_etag_cache(p) == {}


# ─── derive_gh_poll end-to-end ────────────────────────────────────────


@pytest.fixture
def mind(tmp_path, monkeypatch):
    """A clean alice-mind tree + backoff file pinned to tmp."""
    mind = tmp_path / "alice-mind"
    (mind / "inner" / "state").mkdir(parents=True)
    # Pin the backoff file to tmp so the shared-state singleton
    # doesn't leak between tests.
    monkeypatch.setenv(
        "ALICE_GH_RATE_LIMIT_BACKOFF_PATH",
        str(mind / "inner" / "state" / "gh-rate-limit-backoff.json"),
    )
    return mind


def _mock_issues_response(n: int = 2) -> list[dict]:
    return [
        {
            "number": 790,
            "title": "SM v2 → DB",
            "created_at": "2026-11-05T10:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "design body",
            "labels": [
                {"name": "sm:selected"},
                {"name": "art:code"},
            ],
        },
        {
            "number": 802,
            "title": "Step 1 seed",
            "created_at": "2026-11-06T00:00:00Z",
            "updated_at": "2026-11-06T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "labels": [{"name": "sm:design_review"}, {"name": "art:code"}],
        },
    ][:n]


def _mock_prs_response() -> list[dict]:
    return [
        {
            "number": 802,
            "title": "feat: sm seed",
            "created_at": "2026-11-06T09:00:00Z",
            "updated_at": "2026-11-06T18:00:00Z",
            "user": {"login": "jcronq"},
            "body": "Closes #790",
            "state": "closed",
            "merged_at": "2026-11-06T18:30:00Z",
            "draft": False,
        },
        {
            "number": 803,
            "title": "feat: poller",
            "created_at": "2026-11-07T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "state": "open",
            "merged_at": None,
            "draft": False,
        },
    ]


def test_derive_gh_poll_upserts_items_and_state(mind):
    """A happy-path poll writes items + state for issues and PRs."""

    def runner(cmd):
        url = cmd[-1]
        if "/issues" in url and "/pulls" not in url:
            body = json.dumps(_mock_issues_response())
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "etag-issues", body), stderr=""
            )
        if "/pulls" in url:
            body = json.dumps(_mock_prs_response())
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "etag-prs", body), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    report = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},  # no token
        runner=runner,
    )
    assert report.status == "ok", report.errors
    assert report.repos_polled == 1
    assert report.issues_seen == 2
    assert report.prs_seen == 2
    assert report.items_upserted >= 4

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT item_id, item_type, title FROM items ORDER BY item_id"
        ).fetchall()
    ids = [r["item_id"] for r in rows]
    assert "gh:jcronq/alice#790" in ids
    assert "gh:pr:jcronq/alice#802" in ids
    assert "gh:pr:jcronq/alice#803" in ids

    # State for #790 → 'selected'
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            ("gh:jcronq/alice#790",),
        ).fetchone()["state"]
    assert state == "selected"


def test_derive_gh_poll_handles_304_without_rewriting(mind):
    """On a 304 the poller counts the hit but does no SQL writes."""

    db_path = mind / "inner" / "state" / "alice-state.db"
    cache_path = mind / "inner" / "state" / "gh-poll-etags.json"
    cache_path.write_text(
        json.dumps(
            {
                "jcronq/alice:issues": '"etag-issues"',
                "jcronq/alice:pulls": '"etag-prs"',
            }
        )
    )

    def runner(cmd):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(304, "same-etag", ""), stderr=""
        )

    report = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    assert report.status == "ok"
    assert report.not_modified_responses == 2
    assert report.items_upserted == 0
    assert report.issues_seen == 0


def test_derive_gh_poll_honors_backoff(mind):
    """An active backoff window short-circuits the poll."""
    backoff_path = mind / "inner" / "state" / "gh-rate-limit-backoff.json"
    future = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    # Write a far-future backoff.
    state = _rate_limit_mod.BackoffState(
        last_updated=future,
        core_remaining=5000,
        core_reset_at=None,
        graphql_remaining=5000,
        graphql_reset_at=None,
        backoff_until="2099-01-01T00:00:00+00:00",
        backoff_reason="test",
        consecutive_hits=1,
    )
    _rate_limit_mod.write_backoff_state(state, backoff_path)

    called = []

    def runner(cmd):
        called.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    report = P.derive_gh_poll(
        mind,
        repos=["jcronq/alice"],
        backoff_path=backoff_path,
        env={},
        runner=runner,
    )
    assert report.status == "backoff"
    assert report.http_calls == 0
    assert called == []  # no HTTP calls fired


def test_derive_gh_poll_records_rate_limit_hit_in_backoff_file(mind):
    """A 429 ripples out via ``observe_gh_error`` to the backoff file."""
    backoff_path = mind / "inner" / "state" / "gh-rate-limit-backoff.json"

    def runner(cmd):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(429, None, "rate limit"), stderr=""
        )

    report = P.derive_gh_poll(
        mind,
        repos=["jcronq/alice"],
        backoff_path=backoff_path,
        env={},
        runner=runner,
    )
    assert report.status == "rate_limited"
    assert report.rate_limit_hits == 1
    # The backoff state should now carry a backoff_until.
    state = _rate_limit_mod.read_backoff_state(backoff_path)
    assert state.backoff_until is not None


def test_derive_gh_poll_is_idempotent(mind):
    """Two consecutive polls with the same GH response do zero net work the second time."""

    def runner(cmd):
        url = cmd[-1]
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(
                    200, "etag1", json.dumps(_mock_issues_response(1))
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=_mk_curl_output(
                200, "etag2", json.dumps(_mock_prs_response()[:1])
            ),
            stderr="",
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    cache_path = mind / "inner" / "state" / "gh-poll-etags.json"

    r1 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    first_rows = _row_count(db_path, "items")
    r2 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    assert r1.status == "ok"
    assert r2.status == "ok"
    # Row counts should not grow: upserts on items, transitions/amendments
    # are dedup'd by their unique indexes.
    assert _row_count(db_path, "items") == first_rows


def _row_count(db_path: pathlib.Path, table: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ─── issue #821: component bucketing ─────────────────────────────────


def _mock_pr_files(number: int) -> list[dict]:
    """Return a canned file list for one PR.

    #803 touches a single component (``status_site``); #803's alternate
    is used for the multi-component case.
    """
    if number == 803:
        return [
            {"filename": "src/status_site/sources/pipeline.py"},
            {"filename": "src/status_site/static/index.html"},
        ]
    if number == 802:
        return [
            {"filename": "src/alice_forge/dispatcher/main.py"},
            {"filename": "src/alice_state_db/schema.py"},
            {"filename": "sandbox/Dockerfile"},
        ]
    return []


def _runner_for_components(issues=None, prs=None):
    """Runner that serves issues + prs + per-PR file lists."""
    issues_body = json.dumps(issues if issues is not None else _mock_issues_response())
    prs_body = json.dumps(prs if prs is not None else _mock_prs_response())

    def run(cmd):
        url = cmd[-1]
        # Per-PR files endpoint: ``/pulls/<N>/files``
        if "/pulls/" in url and url.rstrip("?").split("?", 1)[0].endswith("/files"):
            import re as _re

            m = _re.search(r"/pulls/(\d+)/files", url)
            number = int(m.group(1)) if m else 0
            body = json.dumps(_mock_pr_files(number))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, f'"files-{number}"', body), stderr=""
            )
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, '"etag-issues"', issues_body), stderr=""
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, '"etag-prs"', prs_body), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    return run


def test_derive_gh_poll_fetches_pr_files_and_writes_components(mind):
    """Poll a repo whose single open PR touches ``status_site``.

    After one pass the PR's ``item_components`` row set equals the
    components the file list implies, and the linked issue inherits
    the same set via ``derive_issue_components``.
    """
    # Both #790 and #802 issues come back open; both PRs link back
    # via ``Closes #<N>`` so their components fan out to the issues.
    runner = _runner_for_components()
    db_path = mind / "inner" / "state" / "alice-state.db"
    report = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},
        runner=runner,
    )
    assert report.status == "ok", report.errors
    assert report.pr_files_fetched == 2
    assert report.component_rows_inserted > 0

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # PR #803 touches only ``status_site``.
        pr_803 = {
            r["component"]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:pr:jcronq/alice#803",),
            ).fetchall()
        }
        assert pr_803 == {"status_site"}
        # PR #802 touches multiple components, including infra.
        pr_802 = {
            r["component"]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:pr:jcronq/alice#802",),
            ).fetchall()
        }
        assert pr_802 == {"alice_forge", "alice_state_db", "infra"}

        # Issue #790 is linked from PR #802 (Closes #790) so inherits
        # that PR's component set.
        iss_790 = {
            r["component"]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:jcronq/alice#790",),
            ).fetchall()
        }
        assert iss_790 == {"alice_forge", "alice_state_db", "infra"}

        # Issue #802 has no PR closing it in the canned fixtures,
        # so it lands in the ``pre-build`` bucket.
        iss_802 = {
            r["component"]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:jcronq/alice#802",),
            ).fetchall()
        }
        assert iss_802 == {"pre-build"}


def test_derive_gh_poll_component_rows_are_idempotent(mind):
    """Re-running the poll inserts zero additional component rows."""
    runner = _runner_for_components()
    db_path = mind / "inner" / "state" / "alice-state.db"
    cache_path = mind / "inner" / "state" / "gh-poll-etags.json"

    r1 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    first_count = _row_count(db_path, "item_components")
    first_inserted = r1.component_rows_inserted
    assert first_inserted > 0

    r2 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    assert r2.status == "ok"
    assert r2.component_rows_inserted == 0
    assert r2.component_rows_deleted == 0
    assert _row_count(db_path, "item_components") == first_count


def test_derive_gh_poll_pre_build_bucket_for_issue_without_pr(mind):
    """An open issue with no linked PR lands in the ``pre-build`` bucket."""
    # Serve one issue (#790) and zero PRs.
    runner = _runner_for_components(
        issues=_mock_issues_response(1),  # just #790
        prs=[],
    )
    db_path = mind / "inner" / "state" / "alice-state.db"
    P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},
        runner=runner,
    )

    with sqlite3.connect(str(db_path)) as conn:
        comps = {
            r[0]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:jcronq/alice#790",),
            ).fetchall()
        }
    assert comps == {"pre-build"}


def test_derive_gh_poll_pr_files_304_leaves_rows(mind):
    """A 304 on the PR files endpoint preserves the prior component rows."""
    runner = _runner_for_components()
    db_path = mind / "inner" / "state" / "alice-state.db"
    cache_path = mind / "inner" / "state" / "gh-poll-etags.json"

    # First pass seeds the DB + caches ETags for every PR files call.
    P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )

    # Second pass: every PR files call returns 304, the issue/pulls
    # endpoints still return 200 with the same payload so item_external
    # refs get re-upserted and the derivation step runs again.
    def runner_304(cmd):
        url = cmd[-1]
        if "/pulls/" in url and url.rstrip("?").split("?", 1)[0].endswith("/files"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(304, None, ""), stderr=""
            )
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(
                    200, '"etag-issues"', json.dumps(_mock_issues_response())
                ),
                stderr="",
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(
                    200, '"etag-prs"', json.dumps(_mock_prs_response())
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    r2 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner_304,
    )
    assert r2.status == "ok"
    # No file list refetched, so no deltas on the PR side.
    assert r2.pr_files_fetched == 0
    with sqlite3.connect(str(db_path)) as conn:
        comps = {
            r[0]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:pr:jcronq/alice#803",),
            ).fetchall()
        }
    assert comps == {"status_site"}


def test_derive_issue_components_reflects_updated_pr_file_list(mind):
    """When a PR's file list changes, the linked issue picks up the delta."""
    db_path = mind / "inner" / "state" / "alice-state.db"
    cache_path = mind / "inner" / "state" / "gh-poll-etags.json"

    # First pass: #803's files are what _mock_pr_files returns.
    P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=_runner_for_components(),
    )

    # Second pass: #803 now touches alice_speaking + status_site.
    def runner(cmd):
        url = cmd[-1]
        if "/pulls/803/files" in url:
            body = json.dumps(
                [
                    {"filename": "src/status_site/sources/pipeline.py"},
                    {"filename": "src/alice_speaking/daemon.py"},
                ]
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, '"files-803-v2"', body), stderr=""
            )
        if "/pulls/802/files" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(304, None, ""), stderr=""
            )
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(
                    200, '"etag-issues"', json.dumps(_mock_issues_response())
                ),
                stderr="",
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(
                    200, '"etag-prs"', json.dumps(_mock_prs_response())
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    r2 = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        etag_cache_path=cache_path,
        env={},
        runner=runner,
    )
    assert r2.status == "ok"
    # PR #803 originally touched {status_site}; it now touches
    # {status_site, alice_speaking}. Row delta is one insertion
    # (alice_speaking); the existing status_site row stays.
    assert r2.component_rows_inserted >= 1

    with sqlite3.connect(str(db_path)) as conn:
        pr_803 = {
            r[0]
            for r in conn.execute(
                "SELECT component FROM item_components WHERE item_id = ?",
                ("gh:pr:jcronq/alice#803",),
            ).fetchall()
        }
    assert pr_803 == {"status_site", "alice_speaking"}


# ─── reconcile_stale_open_prs — merged PR transition ──────────────────


def test_derive_gh_poll_reconciles_merged_prs_that_dropped_off_open_list(mind):
    """A PR previously at ``pr:open`` that no longer appears in the
    ``?state=open`` list gets fetched individually and transitions to
    ``pr:merged`` (plus a cached merged_at amendment). Mirrors the
    staleness that showed up on the Overview after PRs #868-#873 landed
    but their sm rows never advanced past pr:open."""

    db_path = mind / "inner" / "state" / "alice-state.db"

    # Pass 1: open-list contains both PRs. Seeds sm rows at pr:open.
    def runner_open(cmd):
        url = cmd[-1]
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "e1", "[]"), stderr=""
            )
        if url.endswith("/pulls?state=open&per_page=100&sort=updated&direction=desc"):
            prs = [
                {
                    "number": 900,
                    "title": "will merge",
                    "created_at": "2026-11-09T09:00:00Z",
                    "updated_at": "2026-11-09T10:00:00Z",
                    "user": {"login": "jcronq"},
                    "body": "",
                    "state": "open",
                    "merged_at": None,
                    "draft": False,
                },
                {
                    "number": 901,
                    "title": "stays open",
                    "created_at": "2026-11-09T09:00:00Z",
                    "updated_at": "2026-11-09T10:00:00Z",
                    "user": {"login": "jcronq"},
                    "body": "",
                    "state": "open",
                    "merged_at": None,
                    "draft": False,
                },
            ]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "eprs-1", json.dumps(prs)), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},
        runner=runner_open,
    )

    # Confirm both sm rows at pr:open after pass 1.
    with sqlite3.connect(str(db_path)) as conn:
        states = dict(
            conn.execute(
                "SELECT item_id, state FROM state_machine WHERE item_id LIKE 'gh:pr:%'"
            ).fetchall()
        )
    assert states == {
        "gh:pr:jcronq/alice#900": "pr:open",
        "gh:pr:jcronq/alice#901": "pr:open",
    }

    # Clear etag cache so pass 2 doesn't short-circuit on 304.
    (mind / "inner" / "state" / "gh-poll-etags.json").unlink()

    # Pass 2: #900 merged, only #901 is in the open list. The
    # reconciler must fetch /pulls/900 and transition it to pr:merged.
    def runner_merged(cmd):
        url = cmd[-1]
        if "/issues" in url and "/pulls" not in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "e2", "[]"), stderr=""
            )
        if url.endswith("/pulls?state=open&per_page=100&sort=updated&direction=desc"):
            prs = [
                {
                    "number": 901,
                    "title": "stays open",
                    "created_at": "2026-11-09T09:00:00Z",
                    "updated_at": "2026-11-09T10:00:00Z",
                    "user": {"login": "jcronq"},
                    "body": "",
                    "state": "open",
                    "merged_at": None,
                    "draft": False,
                },
            ]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "eprs-2", json.dumps(prs)), stderr=""
            )
        if url.endswith("/pulls/900"):
            pr = {
                "number": 900,
                "title": "will merge",
                "created_at": "2026-11-09T09:00:00Z",
                "updated_at": "2026-11-09T11:00:00Z",
                "user": {"login": "jcronq"},
                "body": "",
                "state": "closed",
                "merged_at": "2026-11-09T10:30:00Z",
                "draft": False,
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, None, json.dumps(pr)), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    report = P.derive_gh_poll(
        mind,
        db_path=db_path,
        repos=["jcronq/alice"],
        env={},
        runner=runner_merged,
    )
    assert report.status == "ok", report.errors
    assert report.stale_prs_reconciled == 1

    with sqlite3.connect(str(db_path)) as conn:
        states = dict(
            conn.execute(
                "SELECT item_id, state FROM state_machine WHERE item_id LIKE 'gh:pr:%'"
            ).fetchall()
        )
        merged_amendment = conn.execute(
            "SELECT body FROM amendments WHERE item_id = ? AND source = 'pr-merged-at'",
            ("gh:pr:jcronq/alice#900",),
        ).fetchone()
    assert states == {
        "gh:pr:jcronq/alice#900": "pr:merged",
        "gh:pr:jcronq/alice#901": "pr:open",
    }
    assert merged_amendment is not None
    assert merged_amendment[0] == "2026-11-09T10:30:00Z"
