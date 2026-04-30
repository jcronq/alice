"""Vault state snapshot — Plan 03 Phase 3.

The selector + sleep-mode sub-stage logic need a few facts about
the mind's current state at wake-start: is the inbox empty, are
there link issues, has Stage D's nightly cap been hit, etc. Today
the agent reads these in the prompt; pre-snapshotting moves the
heuristic to code (where it can be unit-tested) and saves an
agent tool call per wake.

The snapshot is **read-only** — never writes to the mind. It
touches the filesystem but is fast (small directory listings +
one frontmatter scan per file). Phase 3 wires it through the
selector; Phase 4 (deferred — behavior change) consumes the
counter fields for SleepMode sub-stage selection.
"""

from __future__ import annotations

import datetime
import pathlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


# Stage D's nightly synthesis cap. Mirrored from
# ``inner/directive.md`` Step 0; kept here as a constant so tests +
# the selector see the same value. If the operator tunes the cap
# in the directive, sync this constant.
STAGE_D_NIGHTLY_CAP = 3


@dataclass(frozen=True)
class VaultState:
    """Snapshot of mind state read at wake-start. Frozen so the
    selector can rely on stability across calls."""

    # Inbox / structural state.
    has_pending_inbox: bool = False
    has_link_issues: bool = False

    # Vault has no notable pending work — Stage D / Stage C can fire.
    is_stable: bool = True

    # ≥2 research notes in the last 24h. Required input for Stage D
    # eligibility (recombination needs fresh material).
    has_recent_research_corpus: bool = False

    # Adaptive counters — required by Phase 4's SleepMode escape
    # hatches. Phase 3 populates them but doesn't consume them yet
    # (that's a behavior change deferred per the combined plan).
    consecutive_b_wakes: int = 0
    consecutive_null_c_wakes: int = 0
    stage_d_cap_exhausted: bool = False

    # Misc — useful for telemetry / debugging the snapshot.
    orphan_count: int = 0
    last_groomed_ts: Optional[datetime.datetime] = None
    research_corpus_age_days: Optional[float] = None
    raw: dict = field(default_factory=dict)


# Frontmatter shape:
#   ---
#   mode: sleep
#   stage: B
#   did_work: false
#   ---
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the YAML-ish wake-file frontmatter into a flat dict.

    Doesn't pull in PyYAML — the shape is one-line key: value
    entries. Robust against the common case (``did_work: false —
    extra commentary``) by taking only the first token after the
    colon for boolean fields.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        out[key.strip()] = raw_value.strip()
    return out


def _is_falsey(raw: str) -> bool:
    """Robust did_work parser: leading token must be ``false`` or
    ``no`` (case-insensitive). Accepts trailing commentary."""
    if not raw:
        return False
    head = raw.strip().split()[0].lower().rstrip(".,;—-")
    return head in ("false", "no", "0")


def _is_truthy(raw: str) -> bool:
    if not raw:
        return False
    head = raw.strip().split()[0].lower().rstrip(".,;—-")
    return head in ("true", "yes", "1")


def _wake_files_within(
    thoughts_dir: pathlib.Path, *, since: datetime.datetime
) -> list[pathlib.Path]:
    """Return wake files written in the last ``since`` window,
    sorted oldest-first by mtime so callers can walk consecutively
    from the past forward."""
    if not thoughts_dir.is_dir():
        return []
    cutoff = since.timestamp()
    out: list[pathlib.Path] = []
    for day_dir in thoughts_dir.iterdir():
        if not day_dir.is_dir():
            continue
        for f in day_dir.glob("*.md"):
            try:
                if f.stat().st_mtime >= cutoff:
                    out.append(f)
            except OSError:
                continue
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def _consecutive_count(
    files: Iterable[pathlib.Path],
    *,
    stage: str,
    require_did_work_false: bool,
) -> int:
    """Count consecutive wake files matching ``stage`` (and
    optionally ``did_work: false``) from the most-recent backwards.

    "Consecutive" means: walking newest → oldest, count files where
    the frontmatter matches; stop on the first mismatch. If a wake
    file lacks the matching ``stage`` field it breaks the streak.
    """
    streak = 0
    for f in reversed(list(files)):
        try:
            text = f.read_text()
        except OSError:
            break
        fm = _parse_frontmatter(text)
        if fm.get("stage", "").upper() != stage.upper():
            break
        if require_did_work_false and not _is_falsey(fm.get("did_work", "")):
            break
        streak += 1
    return streak


def _stage_d_cap_exhausted_today(
    state_dir: pathlib.Path, *, today: Optional[datetime.date] = None
) -> bool:
    """Has tonight's stage-d-pairs.jsonl reached the nightly cap?

    The pairs log lives at
    ``inner/state/stage-d-pairs-YYYY-MM-DD.jsonl`` — one line per
    Stage D synthesis. Cap is :data:`STAGE_D_NIGHTLY_CAP`.
    """
    today = today or datetime.date.today()
    path = state_dir / f"stage-d-pairs-{today.isoformat()}.jsonl"
    if not path.is_file():
        return False
    try:
        lines = sum(1 for line in path.read_text().splitlines() if line.strip())
    except OSError:
        return False
    return lines >= STAGE_D_NIGHTLY_CAP


def _has_pending_inbox(mind: pathlib.Path) -> bool:
    """``inner/notes/`` is the inbox — any unprocessed note triggers
    Stage B."""
    notes = mind / "inner" / "notes"
    if not notes.is_dir():
        return False
    for f in notes.iterdir():
        if f.is_file() and f.suffix in (".md", ".markdown"):
            return True
    return False


def _has_recent_research_corpus(
    mind: pathlib.Path, *, now: datetime.datetime, window_hours: int = 24
) -> tuple[bool, Optional[float]]:
    """≥2 research notes in the last ``window_hours``. Returns
    ``(eligible, age_days_of_oldest_qualifying_note)``."""
    research = mind / "cortex-memory" / "research"
    if not research.is_dir():
        return (False, None)
    cutoff = (now - datetime.timedelta(hours=window_hours)).timestamp()
    fresh: list[float] = []
    for f in research.rglob("*.md"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt >= cutoff:
            fresh.append(mt)
    if len(fresh) < 2:
        return (False, None)
    oldest = min(fresh)
    age_days = (now.timestamp() - oldest) / 86400.0
    return (True, age_days)


def snapshot(
    mind: pathlib.Path,
    *,
    now: Optional[datetime.datetime] = None,
    consecutive_window_hours: int = 3,
) -> VaultState:
    """Read the mind's current state into a :class:`VaultState`.

    Cheap I/O — listings + a handful of frontmatter reads. Safe to
    call at wake-start regardless of mind shape; missing files just
    leave the corresponding fields at their defaults.
    """
    if now is None:
        now = datetime.datetime.now()

    thoughts_dir = mind / "inner" / "thoughts"
    state_dir = mind / "inner" / "state"
    recent = _wake_files_within(
        thoughts_dir,
        since=now - datetime.timedelta(hours=consecutive_window_hours),
    )

    has_inbox = _has_pending_inbox(mind)
    has_corpus, corpus_age = _has_recent_research_corpus(mind, now=now)
    consecutive_b = _consecutive_count(
        recent, stage="B", require_did_work_false=True
    )
    consecutive_null_c = _consecutive_count(
        recent, stage="C", require_did_work_false=True
    )
    cap_exhausted = _stage_d_cap_exhausted_today(state_dir, today=now.date())

    is_stable = not has_inbox

    return VaultState(
        has_pending_inbox=has_inbox,
        has_link_issues=False,  # no source for this yet; Phase 3 leaves False
        is_stable=is_stable,
        has_recent_research_corpus=has_corpus,
        consecutive_b_wakes=consecutive_b,
        consecutive_null_c_wakes=consecutive_null_c,
        stage_d_cap_exhausted=cap_exhausted,
        research_corpus_age_days=corpus_age,
    )
