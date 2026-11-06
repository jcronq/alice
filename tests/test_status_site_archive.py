"""Done-today -> done-archive.md roll: archive_done, the date-roll tick, status-archive CLI."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from status_site import cli
from status_site import server as S
from status_site.server import Config, archive_done, archive_tick

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=TZ)

INDEX = """# Status

## Waiting on you

- [ ] **Pick a color**: blue or green

## Done today

- [x] **Old win**: finished yesterday
  with a continuation line
- [x] **Fresh win**: finished today
- [x] **Mystery**: no history at all
- [ ] **Still open**: not checked
- [x] **Other old**: also yesterday

## Later

- [ ] **Later thing**
"""

ARCHIVE = """# Done archive

## 2026-11-03

- [x] **Ancient**: long ago
"""


def cfg_for(tmp_path: Path, index: str = INDEX, archive: str | None = ARCHIVE) -> Config:
    (tmp_path / "index.md").write_text(index)
    if archive is not None:
        (tmp_path / "done-archive.md").write_text(archive)
    return Config(
        status_dir=tmp_path, surface_dir=tmp_path, upload_dir=tmp_path, key="k"
    )


def hist(cfg: Config, slug: str, *events: dict) -> None:
    d = cfg.status_dir / "item-history"
    d.mkdir(exist_ok=True)
    (d / f"{slug}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def checked_at(at: str) -> dict:
    return {"at": at, "type": "checked", "page": "index"}


def seed(cfg: Config) -> None:
    hist(cfg, "old-win", checked_at("2026-11-04T16:30:00-04:00"))
    hist(cfg, "fresh-win", checked_at("2026-11-05T08:00:00-04:00"))
    hist(cfg, "other-old", checked_at("2026-11-04T10:00:00-04:00"))


# ---------------------------------------------------------------- completion date


def test_checked_yesterday_archived_today_and_unknown_retained(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    got = archive_done(cfg, NOW)
    assert [(g["slug"], g["date"]) for g in got] == [
        ("old-win", "2026-11-04"),
        ("other-old", "2026-11-04"),
    ]
    idx = (tmp_path / "index.md").read_text()
    assert "Old win" not in idx and "continuation" not in idx
    assert "Other old" not in idx
    for kept in ("Fresh win", "Mystery", "Still open", "Pick a color", "Later thing"):
        assert kept in idx


def test_seeded_appeared_checked_counts_and_uncheck_resets(tmp_path):
    cfg = cfg_for(tmp_path)
    hist(
        cfg,
        "old-win",
        {"at": "2026-11-01T00:00:00-04:00", "type": "appeared", "checked": True, "seeded": True},
    )
    hist(
        cfg,
        "other-old",
        checked_at("2026-11-02T10:00:00-04:00"),
        {"at": "2026-11-02T11:00:00-04:00", "type": "unchecked", "page": "index"},
    )
    got = archive_done(cfg, NOW)
    assert [(g["slug"], g["date"]) for g in got] == [("old-win", "2026-11-01")]


def test_completion_date_uses_now_timezone(tmp_path):
    cfg = cfg_for(tmp_path)
    # 23:30 UTC on the 4th is 19:30 on the 4th at -04:00, but 04:30 on the 5th at +05:00.
    hist(cfg, "old-win", checked_at("2026-11-04T23:30:00+00:00"))
    east = datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=5)))
    assert archive_done(cfg, east) == []
    assert [g["slug"] for g in archive_done(cfg, NOW)] == ["old-win"]


# ---------------------------------------------------------------- archive_done


def test_archive_groups_by_date_newest_first_and_keeps_existing(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    hist(cfg, "mystery", checked_at("2026-10-30T10:00:00-04:00"))
    archive_done(cfg, NOW)
    arch = (tmp_path / "done-archive.md").read_text()
    heads = [l for l in arch.splitlines() if l.startswith("## ")]
    assert heads == ["## 2026-11-04", "## 2026-11-03", "## 2026-10-30"]
    day4 = arch.split("## 2026-11-04")[1].split("## 2026-11-03")[0]
    assert "**Old win**" in day4 and "with a continuation line" in day4
    assert "**Other old**" in day4
    assert "**Ancient**" in arch.split("## 2026-11-03")[1].split("## 2026-10-30")[0]
    assert arch.startswith("# Done archive\n")
    assert arch.endswith("\n") and not arch.endswith("\n\n")


def test_archive_into_existing_date_section(tmp_path):
    cfg = cfg_for(tmp_path)
    hist(cfg, "old-win", checked_at("2026-11-03T10:00:00-04:00"))
    archive_done(cfg, NOW)
    arch = (tmp_path / "done-archive.md").read_text()
    assert arch.count("## 2026-11-03") == 1
    sec = arch.split("## 2026-11-03")[1]
    assert sec.index("**Ancient**") < sec.index("**Old win**")


def test_creates_archive_page_when_missing(tmp_path):
    cfg = cfg_for(tmp_path, archive=None)
    seed(cfg)
    archive_done(cfg, NOW)
    arch = (tmp_path / "done-archive.md").read_text()
    assert arch.startswith("# Done archive\n\n## 2026-11-04\n")


def test_idempotent(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    assert archive_done(cfg, NOW)
    idx, arch = (tmp_path / "index.md").read_text(), (tmp_path / "done-archive.md").read_text()
    assert archive_done(cfg, NOW) == []
    assert (tmp_path / "index.md").read_text() == idx
    assert (tmp_path / "done-archive.md").read_text() == arch


def test_noop_without_done_section_or_candidates(tmp_path):
    cfg = cfg_for(tmp_path, index="# Status\n\n- [x] **Thing**: x\n")
    hist(cfg, "thing", checked_at("2026-11-01T00:00:00-04:00"))
    assert archive_done(cfg, NOW) == []
    assert (tmp_path / "index.md").read_text() == "# Status\n\n- [x] **Thing**: x\n"
    cfg = cfg_for(tmp_path, index="## Done today\n\n")
    assert archive_done(cfg, NOW) == []


def test_no_index_is_noop(tmp_path):
    cfg = Config(status_dir=tmp_path, surface_dir=tmp_path, upload_dir=tmp_path, key="k")
    assert archive_done(cfg, NOW) == []
    assert not (tmp_path / "done-archive.md").exists()


def test_crash_between_writes_duplicates_but_never_loses(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    real = S._atomic_write
    calls = []

    def flaky(path, text):
        calls.append(path.name)
        if path.name == "index.md":
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(S, "_atomic_write", flaky)
    with pytest.raises(OSError):
        archive_done(cfg, NOW)
    assert calls == ["done-archive.md", "index.md"]
    assert "Old win" in (tmp_path / "index.md").read_text()
    assert "Old win" in (tmp_path / "done-archive.md").read_text()
    assert not list(tmp_path.glob(".*.tmp"))
    monkeypatch.setattr(S, "_atomic_write", real)
    archive_done(cfg, NOW)  # retry: index drained, archive not double-written
    assert (tmp_path / "done-archive.md").read_text().count("**Old win**") == 1
    assert "Old win" not in (tmp_path / "index.md").read_text()


def test_no_temp_files_left_behind(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    archive_done(cfg, NOW)
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


# ---------------------------------------------------------------- date roll


def test_tick_fires_once_per_local_date(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    first = archive_tick(cfg, NOW)
    assert first and len(first) == 2
    state = json.loads((tmp_path / ".archive-state.json").read_text())
    assert state["last_run"] == "2026-11-05"
    # Same day, even with a new stale item: not re-run.
    (tmp_path / "index.md").write_text(INDEX)
    assert archive_tick(cfg, NOW + timedelta(hours=5)) is None
    assert "Old win" in (tmp_path / "index.md").read_text()
    # Next local midnight rolls the date: runs again.
    assert archive_tick(cfg, NOW + timedelta(days=1)) is not None
    assert "Old win" not in (tmp_path / "index.md").read_text()


def test_tick_catches_up_after_missed_midnight(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    (tmp_path / ".archive-state.json").write_text(json.dumps({"last_run": "2026-11-02"}))
    assert archive_tick(cfg, NOW) is not None  # restart days later still fires


def test_tick_survives_corrupt_state(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    (tmp_path / ".archive-state.json").write_text("not json")
    assert archive_tick(cfg, NOW)


def test_roller_loop_runs_ticks_and_stops(tmp_path):
    cfg = cfg_for(tmp_path)
    seed(cfg)
    stop = threading.Event()
    times = iter([NOW, NOW, NOW + timedelta(days=1)])
    seen = []

    def clock():
        t = next(times)
        seen.append(t)
        if len(seen) == 3:
            stop.set()
        return t

    S.run_archive_roller(cfg, interval=0, stop=stop, clock=clock)
    assert len(seen) == 3
    assert "Old win" not in (tmp_path / "index.md").read_text()


def test_state_file_not_served(tmp_path):
    assert not S.CONTENT_NAME_RE.fullmatch(".archive-state.json")


# ---------------------------------------------------------------- CLI


def test_cli_status_archive(tmp_path, monkeypatch, capsys):
    cfg = cfg_for(tmp_path)
    hist(cfg, "old-win", checked_at("2020-01-01T10:00:00-04:00"))
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    assert cli.main_archive([]) == 0
    out = capsys.readouterr().out
    assert "archived old-win -> 2020-01-01" in out and "archived 1 item(s)" in out
    assert "## 2020-01-01" in (tmp_path / "done-archive.md").read_text()
    assert cli.main_archive(["extra"]) == 2


def test_frontend_collapses_done_today():
    html = (Path(S.__file__).parent / "static" / "index.html").read_text()
    assert "function collapseDone" in html
    assert "linkLeaves();collapseDone();" in html
    assert "details.done" in html
