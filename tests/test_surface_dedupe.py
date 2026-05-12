"""Same-type/same-day dedupe in SurfaceWatcher intake.

The Stage D invariant scan and other cron-driven watchers can re-fire
identical surfaces multiple times per day with no new signal. The
intake filter suppresses the second-and-onward occurrence of an
eligible ``surface_type`` per day, auto-archiving with a verdict.

Additional id-based dedup catches same-id same-day re-issues across
arbitrary surface kinds via a JSONL state log under
``inner/state/surface-intake-dedup.jsonl``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import textwrap

import pytest

from alice_speaking.internal.surfaces import (
    DEDUPE_BY_TYPE_PER_DAY,
    ID_DEDUP_WINDOW_HOURS,
    SurfaceWatcher,
    _dedup_key,
    _read_surface_type,
)


def _write_surface(
    surface_dir: pathlib.Path,
    *,
    timestamp: str,
    surface_type: str,
    body: str = "body text",
) -> pathlib.Path:
    name = f"2026-05-12-{timestamp}-{surface_type}.md"
    path = surface_dir / name
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            priority: insight
            surface_type: {surface_type}
            reply_expected: false
            ---

            {body}
            """
        )
    )
    return path


@pytest.fixture
def watcher(tmp_path: pathlib.Path) -> SurfaceWatcher:
    w = SurfaceWatcher(tmp_path)
    w.surface_dir.mkdir(parents=True, exist_ok=True)
    w.handled_dir.mkdir(parents=True, exist_ok=True)
    return w


def test_eligible_type_has_no_prior_today_falls_through(watcher: SurfaceWatcher):
    eligible = next(iter(DEDUPE_BY_TYPE_PER_DAY))
    path = _write_surface(watcher.surface_dir, timestamp="010000", surface_type=eligible)
    assert watcher._suppress_as_duplicate(path) is False
    assert path.exists(), "first occurrence must remain queued for dispatch"


def test_eligible_type_with_prior_today_gets_suppressed(watcher: SurfaceWatcher):
    eligible = next(iter(DEDUPE_BY_TYPE_PER_DAY))
    today = datetime.date.today().isoformat()
    prior_dir = watcher.handled_dir / today
    prior_dir.mkdir(parents=True, exist_ok=True)
    prior = prior_dir / f"2026-05-12-010000-{eligible}.md"
    prior.write_text(f"---\nsurface_type: {eligible}\n---\nprior\n")

    new = _write_surface(watcher.surface_dir, timestamp="030000", surface_type=eligible)

    assert watcher._suppress_as_duplicate(new) is True
    assert not new.exists(), "suppressed surface must be moved out of surface_dir"
    archived = prior_dir / new.name
    assert archived.exists()
    contents = archived.read_text()
    assert "duplicate-suppressed-by-intake" in contents
    assert prior.name in contents


def test_non_eligible_type_with_prior_today_falls_through(watcher: SurfaceWatcher):
    today = datetime.date.today().isoformat()
    prior_dir = watcher.handled_dir / today
    prior_dir.mkdir(parents=True, exist_ok=True)
    prior = prior_dir / "2026-05-12-010000-some-llm-insight.md"
    prior.write_text("---\nsurface_type: some-llm-insight\n---\n")

    new = _write_surface(
        watcher.surface_dir,
        timestamp="030000",
        surface_type="some-llm-insight",
    )

    assert watcher._suppress_as_duplicate(new) is False
    assert new.exists(), "non-eligible types must not be filtered by intake"


def test_missing_frontmatter_falls_through(watcher: SurfaceWatcher):
    path = watcher.surface_dir / "2026-05-12-040000-stray.md"
    path.write_text("no frontmatter here, just body text")
    assert watcher._suppress_as_duplicate(path) is False
    assert path.exists()


def test_read_surface_type_handles_quoted_value(tmp_path: pathlib.Path):
    path = tmp_path / "s.md"
    path.write_text('---\nsurface_type: "stage-d-invariant"\n---\nbody\n')
    assert _read_surface_type(path) == "stage-d-invariant"


def test_eligible_type_ignores_priors_from_other_days(watcher: SurfaceWatcher):
    eligible = next(iter(DEDUPE_BY_TYPE_PER_DAY))
    other_day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    other_dir = watcher.handled_dir / other_day
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / f"2026-05-11-010000-{eligible}.md").write_text(
        f"---\nsurface_type: {eligible}\n---\nyesterday\n",
    )

    new = _write_surface(watcher.surface_dir, timestamp="050000", surface_type=eligible)
    assert watcher._suppress_as_duplicate(new) is False
    assert new.exists()


# ---------------------------------------------------------------------------
# Id-based same-day dedup (issue #120)
# ---------------------------------------------------------------------------


def _write_issue_surface(
    surface_dir: pathlib.Path,
    *,
    timestamp: str,
    slug: str,
    body: str = "issue body",
) -> pathlib.Path:
    name = f"2026-05-12-{timestamp}-{slug}.md"
    path = surface_dir / name
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            priority: insight
            reply_expected: true
            ---

            {body}
            """
        )
    )
    return path


def _dedup_log(watcher: SurfaceWatcher) -> pathlib.Path:
    return watcher._dedup_log_path


def _record_prior(
    watcher: SurfaceWatcher,
    *,
    key: str,
    filename: str,
    when: datetime.datetime,
) -> None:
    log_path = _dedup_log(watcher)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": when.isoformat(timespec="seconds"),
                    "key": key,
                    "filename": filename,
                }
            )
            + "\n"
        )


def test_dedup_key_uses_filename_slug_and_date(tmp_path: pathlib.Path):
    path = tmp_path / "2026-05-12-155100-issue-dispatch-alice-115.md"
    path.write_text("---\npriority: insight\n---\nbody\n")
    assert _dedup_key(path) == "issue-dispatch-alice-115|2026-05-12"


def test_dedup_key_prefers_frontmatter_overrides(tmp_path: pathlib.Path):
    path = tmp_path / "2026-05-12-155100-issue-dispatch-alice-115.md"
    path.write_text(
        "---\nsource-id: alice-issue-115\ndate: 2026-05-12\n---\nbody\n"
    )
    assert _dedup_key(path) == "alice-issue-115|2026-05-12"


def test_dedup_key_returns_none_for_unparseable_filename(tmp_path: pathlib.Path):
    path = tmp_path / "weirdo.md"
    path.write_text("no frontmatter, no date\n")
    assert _dedup_key(path) is None


def test_id_dedup_first_occurrence_records_and_falls_through(
    watcher: SurfaceWatcher,
):
    path = _write_issue_surface(
        watcher.surface_dir, timestamp="155100", slug="issue-dispatch-alice-115"
    )
    assert watcher._suppress_as_id_duplicate(path) is False
    assert path.exists(), "first occurrence must remain queued for dispatch"

    log_path = _dedup_log(watcher)
    assert log_path.is_file()
    lines = [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["key"] == "issue-dispatch-alice-115|2026-05-12"
    assert lines[0]["filename"] == path.name


def test_id_dedup_within_window_with_prior_handled_auto_resolves(
    watcher: SurfaceWatcher,
):
    today = datetime.date.today().isoformat()
    handled = watcher.handled_dir / today
    handled.mkdir(parents=True, exist_ok=True)
    prior_name = "2026-05-12-155100-issue-dispatch-alice-115.md"
    (handled / prior_name).write_text(
        "---\npriority: insight\n---\nprior body\n---\nresolved: earlier\n"
    )
    _record_prior(
        watcher,
        key="issue-dispatch-alice-115|2026-05-12",
        filename=prior_name,
        when=datetime.datetime.now().astimezone() - datetime.timedelta(minutes=2),
    )

    new = _write_issue_surface(
        watcher.surface_dir, timestamp="155400", slug="issue-dispatch-alice-115"
    )
    assert watcher._suppress_as_id_duplicate(new) is True
    assert not new.exists(), "suppressed surface moved out of surface_dir"
    archived = handled / new.name
    assert archived.exists()
    contents = archived.read_text()
    assert "let-pass (intake-side dedup)" in contents
    assert prior_name in contents
    assert ".handled/" in contents


def test_id_dedup_within_window_with_prior_pending_defers(
    watcher: SurfaceWatcher,
):
    # Prior surface still in surface_dir (pending), not in .handled/.
    prior_name = "2026-05-12-160000-waist-logging-reminder.md"
    (watcher.surface_dir / prior_name).write_text(
        "---\npriority: insight\n---\nprior body\n"
    )
    _record_prior(
        watcher,
        key="waist-logging-reminder|2026-05-12",
        filename=prior_name,
        when=datetime.datetime.now().astimezone() - datetime.timedelta(minutes=1),
    )

    new = _write_issue_surface(
        watcher.surface_dir, timestamp="160500", slug="waist-logging-reminder"
    )
    assert watcher._suppress_as_id_duplicate(new) is True
    assert not new.exists(), "deferred surface moved out of surface_dir"
    today = datetime.date.today().isoformat()
    archived = watcher.handled_dir / today / new.name
    assert archived.exists()
    contents = archived.read_text()
    assert "deferred-pending-prior" in contents
    assert prior_name in contents


def test_id_dedup_outside_window_falls_through(watcher: SurfaceWatcher):
    today = datetime.date.today().isoformat()
    handled = watcher.handled_dir / today
    handled.mkdir(parents=True, exist_ok=True)
    prior_name = "2026-05-12-100000-cue-runner-preprocessing.md"
    (handled / prior_name).write_text("---\n---\nprior body\n")
    _record_prior(
        watcher,
        key="cue-runner-preprocessing|2026-05-12",
        filename=prior_name,
        when=datetime.datetime.now().astimezone()
        - datetime.timedelta(hours=ID_DEDUP_WINDOW_HOURS + 1),
    )

    new = _write_issue_surface(
        watcher.surface_dir, timestamp="200000", slug="cue-runner-preprocessing"
    )
    assert watcher._suppress_as_id_duplicate(new) is False
    assert new.exists(), "stale prior outside window must not dedup"


def test_id_dedup_surface_with_no_id_falls_through(watcher: SurfaceWatcher):
    path = watcher.surface_dir / "weirdo.md"
    path.write_text("no frontmatter, no recognisable name\n")
    assert watcher._suppress_as_id_duplicate(path) is False
    assert path.exists()


def test_id_dedup_state_survives_restart(tmp_path: pathlib.Path):
    # Phase 1: a watcher records a first occurrence.
    w1 = SurfaceWatcher(tmp_path)
    w1.surface_dir.mkdir(parents=True, exist_ok=True)
    w1.handled_dir.mkdir(parents=True, exist_ok=True)
    first = _write_issue_surface(
        w1.surface_dir, timestamp="120000", slug="issue-dispatch-alice-116"
    )
    assert w1._suppress_as_id_duplicate(first) is False

    # Move the first into .handled/ as if it had dispatched and resolved.
    today = datetime.date.today().isoformat()
    handled = w1.handled_dir / today
    handled.mkdir(parents=True, exist_ok=True)
    first.rename(handled / first.name)

    # Phase 2: fresh watcher (restart) sees a duplicate — must still dedup.
    w2 = SurfaceWatcher(tmp_path)
    second = _write_issue_surface(
        w2.surface_dir, timestamp="120500", slug="issue-dispatch-alice-116"
    )
    assert w2._suppress_as_id_duplicate(second) is True
    archived = w2.handled_dir / today / second.name
    assert archived.exists()
    contents = archived.read_text()
    assert "let-pass (intake-side dedup)" in contents
