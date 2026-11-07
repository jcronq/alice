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


# ─── Issue #821: PR-files → touches_component refs ──────────────────


def _files_response(filenames: list[str]) -> list[dict]:
    return [{"filename": f} for f in filenames]


def test_poll_writes_touches_component_rows_from_pr_files(mind):
    """A PR with ``Closes #42`` touching ``src/status_site/foo.py``
    writes a ``touches_component`` row on the issue's item_id."""
    issues = [
        {
            "number": 42,
            "title": "layout tweak",
            "created_at": "2026-11-06T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "labels": [{"name": "sm:building"}],
        }
    ]
    prs = [
        {
            "number": 900,
            "title": "fix layout",
            "created_at": "2026-11-07T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "Closes #42",
            "state": "open",
            "merged_at": None,
            "draft": False,
        }
    ]
    files_calls = {"n": 0}

    def runner(cmd):
        url = cmd[-1]
        if "/pulls/900/files" in url:
            files_calls["n"] += 1
            body = json.dumps(_files_response(["src/status_site/server.py"]))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "files-etag-1", body), stderr=""
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "prs-etag", json.dumps(prs)), stderr=""
            )
        if "/issues" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(200, "issues-etag", json.dumps(issues)),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    report = P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    assert report.status == "ok", report.errors
    assert files_calls["n"] == 1

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT item_id, ref_target_item_id, ref_source
            FROM item_external_refs
            WHERE ref_type = 'touches_component'
            """
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == "gh:jcronq/alice#42"
    assert row["ref_target_item_id"] == "component:status_site"
    assert row["ref_source"].startswith("pr-files:")
    assert "gh:pr:jcronq/alice#900" in row["ref_source"]


def test_poll_skips_pr_files_when_updated_at_unchanged(mind):
    """A re-poll of a PR with the same ``updated_at`` does NOT hit the
    files endpoint again."""
    issues = [
        {
            "number": 42,
            "title": "layout tweak",
            "created_at": "2026-11-06T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "labels": [{"name": "sm:building"}],
        }
    ]
    prs = [
        {
            "number": 900,
            "title": "fix layout",
            "created_at": "2026-11-07T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "Closes #42",
            "state": "open",
            "merged_at": None,
            "draft": False,
        }
    ]
    files_calls = {"n": 0}

    def runner(cmd):
        url = cmd[-1]
        if "/pulls/900/files" in url:
            files_calls["n"] += 1
            body = json.dumps(_files_response(["src/status_site/server.py"]))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "files-etag-1", body), stderr=""
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "prs-etag", json.dumps(prs)), stderr=""
            )
        if "/issues" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(200, "issues-etag", json.dumps(issues)),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    # First poll fires the files endpoint.
    r1 = P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    assert r1.status == "ok", r1.errors
    assert files_calls["n"] == 1

    # Second poll with same response — files endpoint must NOT fire again.
    r2 = P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    assert r2.status == "ok", r2.errors
    assert files_calls["n"] == 1


def test_poll_fan_out_multi_component_pr(mind):
    """A PR touching multiple components lands one row per bucket."""
    issues = [
        {
            "number": 42,
            "title": "multi-touch",
            "created_at": "2026-11-06T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "labels": [{"name": "sm:building"}],
        }
    ]
    prs = [
        {
            "number": 901,
            "title": "both",
            "created_at": "2026-11-07T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "Closes #42",
            "state": "open",
            "merged_at": None,
            "draft": False,
        }
    ]

    def runner(cmd):
        url = cmd[-1]
        if "/pulls/901/files" in url:
            body = json.dumps(
                _files_response(
                    [
                        "src/status_site/server.py",
                        "src/alice_state_db/gh_poll.py",
                        "sandbox/docker-compose.yml",
                        "tests/test_x.py",
                    ]
                )
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "e", body), stderr=""
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "prs-etag", json.dumps(prs)), stderr=""
            )
        if "/issues" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(200, "issues-etag", json.dumps(issues)),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    with sqlite3.connect(str(db_path)) as conn:
        comps = {
            r[0]
            for r in conn.execute(
                "SELECT ref_target_item_id FROM item_external_refs WHERE ref_type='touches_component'"
            )
        }
    assert comps == {
        "component:status_site",
        "component:alice_state_db",
        "component:infra",
    }


def test_poll_component_label_on_issue_writes_fallback_row(mind):
    """An issue carrying ``component:alice_forge`` label gets a row with
    ``ref_source='issue-label'`` and no extra GH call."""
    issues = [
        {
            "number": 50,
            "title": "needs forge",
            "created_at": "2026-11-05T10:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "",
            "labels": [
                {"name": "sm:selected"},
                {"name": "component:alice_forge"},
            ],
        }
    ]
    files_calls = {"n": 0}

    def runner(cmd):
        url = cmd[-1]
        if "/files" in url:
            files_calls["n"] += 1
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "e", "[]"), stderr=""
            )
        if "/issues" in url:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=_mk_curl_output(200, "issues-etag", json.dumps(issues)),
                stderr="",
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "prs-etag", "[]"), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    assert files_calls["n"] == 0

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT item_id, ref_target_item_id, ref_source
            FROM item_external_refs
            WHERE ref_type = 'touches_component'
            """
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "gh:jcronq/alice#50"
    assert rows[0]["ref_target_item_id"] == "component:alice_forge"
    assert rows[0]["ref_source"] == "issue-label"


def test_poll_skips_pr_files_when_closes_target_unknown(mind):
    """A PR whose ``Closes #N`` points at an issue not in the local DB
    still runs the files endpoint, but the FK-guarded insert is a
    silent no-op — no row written and no crash."""
    # Only the PR is in the response; the issue it closes is not.
    prs = [
        {
            "number": 902,
            "title": "fix ancient thing",
            "created_at": "2026-11-07T09:00:00Z",
            "updated_at": "2026-11-07T10:00:00Z",
            "user": {"login": "jcronq"},
            "body": "Closes #9999",
            "state": "open",
            "merged_at": None,
            "draft": False,
        }
    ]

    def runner(cmd):
        url = cmd[-1]
        if "/pulls/902/files" in url:
            body = json.dumps(_files_response(["src/status_site/server.py"]))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "e", body), stderr=""
            )
        if "/pulls" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "prs-etag", json.dumps(prs)), stderr=""
            )
        if "/issues" in url:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=_mk_curl_output(200, "issues-etag", "[]"), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_mk_curl_output(200, None, "[]"), stderr=""
        )

    db_path = mind / "inner" / "state" / "alice-state.db"
    report = P.derive_gh_poll(
        mind, db_path=db_path, repos=["jcronq/alice"], env={}, runner=runner
    )
    assert report.status == "ok", report.errors

    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM item_external_refs WHERE ref_type='touches_component'"
        ).fetchone()[0]
    assert count == 0
