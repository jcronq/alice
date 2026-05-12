"""Same-type/same-day dedupe in SurfaceWatcher intake.

The Stage D invariant scan and other cron-driven watchers can re-fire
identical surfaces multiple times per day with no new signal. The
intake filter suppresses the second-and-onward occurrence of an
eligible ``surface_type`` per day, auto-archiving with a verdict.
"""

from __future__ import annotations

import datetime
import pathlib
import textwrap

import pytest

from alice_speaking.internal.surfaces import (
    DEDUPE_BY_TYPE_PER_DAY,
    SurfaceWatcher,
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
