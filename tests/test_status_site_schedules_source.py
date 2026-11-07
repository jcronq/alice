"""Tests for the status-site Schedules source (issue #810, Phase 1).

Covers:

* ``active_crons_path`` respects ``ACTIVE_CRONS_PATH`` /
  ``ALICE_MIND`` overrides.
* ``humanize_cron`` turns common 5-field patterns into readable
  summaries and falls back to the raw expression otherwise.
* ``read_jobs``: missing / malformed file → ``[]``; prompts (and
  any ``FORBIDDEN_FIELDS``) are stripped on read even if the writer
  leaked one; rows sort by ``next_run_at`` ascending.
* ``render`` produces the markdown fallback + staleness footer.
* ``section_renderers`` wires a ``schedules`` key that surfaces the
  typed ``jobs`` payload for the SSE / frontend contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from status_site.server import Config, section_renderers
from status_site.sources import schedules as S


NOW = 1_800_000_000.0  # matches other status_site source tests


# ─── path resolution ─────────────────────────────────────────────────


def test_active_crons_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ACTIVE_CRONS_PATH", raising=False)
    monkeypatch.setenv("ALICE_MIND", str(tmp_path))
    p = S.active_crons_path()
    assert p == tmp_path / "inner" / "state" / "active-crons.json"


def test_active_crons_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ACTIVE_CRONS_PATH", str(tmp_path / "custom.json"))
    assert S.active_crons_path() == tmp_path / "custom.json"


# ─── cron humaniser ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("*/5 * * * *", "every 5 min"),
        ("*/1 * * * *", "every minute"),
        ("0 * * * *", "hourly"),
        ("15 * * * *", "hourly at :15"),
        ("0 */2 * * *", "every 2h"),
        ("0 */1 * * *", "hourly"),
        ("30 9 * * *", "daily at 09:30"),
        ("0 0 * * *", "daily at 00:00"),
        ("0 9 * * 1", "weekly on Mon at 09:00"),
        ("0 9 1 * *", "monthly on day 1 at 09:00"),
        ("", ""),
        ("bogus", "bogus"),
        # 6-field seconds-leading cron — strip the leading seconds.
        ("0 */5 * * * *", "every 5 min"),
    ],
)
def test_humanize_cron_patterns(expr, expected):
    assert S.humanize_cron(expr) == expected


def test_humanize_cron_year_trailing_six_field():
    # "0 30 9 * * 2026" — year-trailing 6-field; trims last.
    assert S.humanize_cron("30 9 * * * 2026") == "daily at 09:30"


# ─── reader ──────────────────────────────────────────────────────────


def _write_snapshot(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": "2026-11-07T15:00:00Z", "jobs": jobs}))


def test_read_jobs_missing_file(tmp_path):
    assert S.read_jobs(tmp_path / "nope.json", now=NOW) == []


def test_read_jobs_malformed_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert S.read_jobs(p, now=NOW) == []


def test_read_jobs_non_dict_root(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps(["not", "object"]))
    assert S.read_jobs(p, now=NOW) == []


def test_read_jobs_strips_forbidden_fields(tmp_path):
    p = tmp_path / "crons.json"
    _write_snapshot(
        p,
        [
            {
                "id": "job-1",
                "cron": "*/5 * * * *",
                "description": "babysit PRs",
                "prompt": "/babysit-prs --all",  # MUST be stripped
                "prompt_preview": "dangerous preview",  # MUST be stripped
                "next_run_at": "2026-11-07T15:05:00Z",
                "last_run_at": "2026-11-07T15:00:00Z",
                "last_run_outcome": "ok",
            }
        ],
    )
    rows = S.read_jobs(p, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert "prompt" not in r
    assert "prompt_preview" not in r
    assert r["id"] == "job-1"
    assert r["schedule_summary"] == "every 5 min"
    assert r["description"] == "babysit PRs"
    assert r["last_run_outcome"] == "ok"


def test_read_jobs_sorts_by_next_run_at(tmp_path):
    p = tmp_path / "crons.json"
    now_epoch = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    _write_snapshot(
        p,
        [
            {"id": "late", "cron": "*/5 * * * *", "next_run_at": "2026-11-07T16:00:00Z"},
            {"id": "soon", "cron": "*/1 * * * *", "next_run_at": "2026-11-07T15:01:00Z"},
            {"id": "unknown", "cron": "0 9 * * *"},  # no next_run_at
        ],
    )
    rows = S.read_jobs(p, now=now_epoch)
    assert [r["id"] for r in rows] == ["soon", "late", "unknown"]
    # Countdown labels match shape.
    assert rows[0]["next_run_label"].startswith("in ")
    assert rows[0]["next_run_seconds"] == pytest.approx(60.0, abs=1.0)
    assert rows[2]["next_run_label"] == ""


def test_read_jobs_skips_id_less_entries(tmp_path):
    p = tmp_path / "crons.json"
    _write_snapshot(p, [{"cron": "*/5 * * * *"}, {"id": "", "cron": "*/5 * * * *"}])
    assert S.read_jobs(p, now=NOW) == []


def test_read_jobs_handles_overdue(tmp_path):
    p = tmp_path / "crons.json"
    now_epoch = datetime(2026, 11, 7, 15, 0, 0, tzinfo=timezone.utc).timestamp()
    _write_snapshot(
        p,
        [{"id": "late", "cron": "*/5 * * * *", "next_run_at": "2026-11-07T14:55:00Z"}],
    )
    rows = S.read_jobs(p, now=now_epoch)
    assert rows[0]["next_run_label"] == "overdue"
    assert rows[0]["next_run_seconds"] < 0


# ─── renderer ────────────────────────────────────────────────────────


def test_render_empty_has_placeholder_and_footer():
    out = S.render([])
    assert "*(no scheduled jobs)*" in out
    assert "source: schedules" in out


def test_render_bullet_shape():
    rows = [
        {
            "id": "job-1",
            "cron": "*/5 * * * *",
            "schedule_summary": "every 5 min",
            "description": "babysit PRs",
            "next_run_label": "in 2m",
            "last_run_outcome": "ok",
        }
    ]
    out = S.render(rows)
    assert "**job-1**" in out
    assert "every 5 min" in out
    assert "babysit PRs" in out
    assert "in 2m" in out
    assert "last: ok" in out


# ─── section_renderers wiring ────────────────────────────────────────


def test_section_renderers_exposes_schedules(tmp_path, monkeypatch):
    # Point the source at a tmp snapshot so the renderer finds a file
    # independent of the dev host's ~/alice-mind.
    snap = tmp_path / "crons.json"
    _write_snapshot(
        snap,
        [
            {
                "id": "job-abc",
                "cron": "*/5 * * * *",
                "description": "demo",
                "next_run_at": "2099-01-01T00:00:00Z",
                "last_run_outcome": "ok",
                "prompt": "do not leak",
            }
        ],
    )
    monkeypatch.setenv("ACTIVE_CRONS_PATH", str(snap))
    cfg = Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surfaces",
        upload_dir=tmp_path / "uploads",
        key="k",
    )
    cfg.status_dir.mkdir()
    renderers = section_renderers(cfg)
    assert "schedules" in renderers
    payload = renderers["schedules"]()
    assert payload["jobs"]
    assert payload["jobs"][0]["id"] == "job-abc"
    assert "prompt" not in payload["jobs"][0]
    assert "no scheduled jobs" not in payload["markdown"]
    assert "**job-abc**" in payload["markdown"]


def test_section_renderers_schedules_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIVE_CRONS_PATH", str(tmp_path / "missing.json"))
    cfg = Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surfaces",
        upload_dir=tmp_path / "uploads",
        key="k",
    )
    cfg.status_dir.mkdir()
    payload = section_renderers(cfg)["schedules"]()
    assert payload["jobs"] == []
    assert "*(no scheduled jobs)*" in payload["markdown"]
