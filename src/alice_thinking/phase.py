"""Phase routing — Phase enum, VaultSnapshot, PhaseConfig, selector, fragment loader.

Design: ``cortex-memory/research/2026-05-07-thinking-phase-routing-design.md``.

The ``select_phase()`` function is a deterministic cascade — clock + observable
vault state, no model calls. Phases:

- ``ACTIVE`` (07:00–22:59)
- ``SLEEP_B`` / ``SLEEP_C`` / ``SLEEP_D`` (23:00–06:59 sub-stages)
- ``QUICK`` (smoke test)
- ``DESIGN_COMMISSION`` (task-type dispatched, not cadence-driven)
- ``CONFLICT_RESOLUTION`` (task-type dispatched, vault-state driven —
  open items in ``cortex-memory/conflicts/``, not cadence-driven)

Migration phases (per the design doc):

- **Phase 0 (PR #14)**: full cascade implemented, but
  ``PhaseConfig.enable_full_sleep_dispatch=False`` collapses Sleep to
  ``SLEEP_B`` only, preserving the legacy single-stage behavior.
- **Phase 3 (this commit)**: default flips to
  ``enable_full_sleep_dispatch=True`` — sleep wakes route to
  B/C/D based on vault state. Override via
  ``alice.config.json thinking.phase_routing.enable_full_sleep_dispatch``
  (set ``false`` to fall back to Phase-0 behavior).

The fragment loader (:class:`PromptFragmentLoader`) reads from package
resources at ``alice_thinking/prompts/{prelude.md, active.md, sleep-b.md,
sleep-c.md, sleep-d.md}``. The repo is bind-mounted rw into worker
containers, so edits on the host take effect on the next wake.
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Optional


__all__ = [
    "Phase",
    "VaultSnapshot",
    "PhaseConfig",
    "PromptFragmentLoader",
    "select_phase",
    "build_vault_snapshot",
    "detect_commission_notes",
    "detect_conflict_notes",
    "STAGE_D_NIGHTLY_CAP",
    "STAGE_C_DEBT_ESCALATION_THRESHOLD",
]


# Mirrors :data:`alice_thinking.vault_state.STAGE_D_NIGHTLY_CAP`. Re-declared
# here so ``select_phase()`` doesn't import the legacy snapshot module.
STAGE_D_NIGHTLY_CAP = 5


# Issue #388 — debt-weighted Stage C escalation threshold.
#
# When ``stage_c_candidates.total`` (bloated notes + stale dailies that
# Stage C atomizes / archives) is at or above this value, Rule 2b
# (``consecutive_b >= 6`` loop-break) prefers Stage C over Stage D even
# when a research corpus exists. Without this gate, the corpus-driven
# D-preference always won — Stage C ran zero times across 40 sleep-phase
# wakes between Phase 3 deployment (2026-05-08) and the surface
# (2026-05-26) while vault debt climbed to 10 bloated notes.
#
# Threshold rationale: 5 is the Stage D nightly cap, so the two stages
# are scheduled on comparable workload signals. Lower would over-fire C;
# higher would let debt accumulate indefinitely (which is the bug).
# Self-correcting: once C drains debt below 5, D-preference resumes.
#
# Design: cortex-memory/research/2026-04-26-adaptive-stage-selection-design.md
STAGE_C_DEBT_ESCALATION_THRESHOLD = 5


class Phase(enum.Enum):
    """Phase the harness picked for this wake.

    Order is part of the contract — the cascade in :func:`select_phase`
    short-circuits in the order rules are written, not by enum order.
    """

    ACTIVE = "active"
    SLEEP_B = "sleep_b"
    SLEEP_C = "sleep_c"
    SLEEP_D = "sleep_d"
    QUICK = "quick"
    DESIGN_COMMISSION = "design_commission"
    CONFLICT_RESOLUTION = "conflict_resolution"
    # Task-type dispatched (not cadence-driven). Invoked explicitly by
    # ``_QwenReviser`` inside the design-commission loop to drive a
    # single revision turn through the standard PhaseRunner path.
    REVISE = "revise"
    # Per-issue, stimulus-spawned modes (sub-issue 3 of SM v2 pipeline
    # revision, ``[[2026-05-13-sm-v2-pipeline-revision]]``). The
    # thinking-agent is spawned by
    # :func:`alice_forge.dispatcher.spawn_thinking_agent` to handle one
    # ``(sm:selected, art:code)`` issue end-to-end: design first, then
    # — after Speaking's review + a compaction step — build. Both
    # phases share the spawn dir; the entrypoint script
    # (:mod:`scripts.sm-thinking-perissue` / ``alice/scripts/sm-thinking-perissue.py``)
    # dispatches into the right one from the prompt's ``phase``
    # frontmatter.
    PER_ISSUE_DESIGN = "per_issue_design"
    PER_ISSUE_BUILD = "per_issue_build"


@dataclass(frozen=True)
class VaultSnapshot:
    """Observable mind state used by :func:`select_phase`.

    The snapshot reads the filesystem only — no SQL, no model calls. Built
    by :func:`build_vault_snapshot` at wake-start.
    """

    hour: int
    minute: int
    has_inbox_items: bool
    has_broken_links: bool
    has_orphan_stubs: bool
    has_recent_research: bool
    consecutive_b: int
    consecutive_null_c: int
    stage_d_cap_exhausted: bool
    hours_since_last_d: float  # hours since last D wake; inf if none
    vault_dir_mtime: float
    state_dir: pathlib.Path
    today: str
    # Issue #388 — count of Stage C atomization/archive candidates
    # (bloated notes + stale dailies). Drives Rule 2b debt-weighted
    # escalation: when this is at or above
    # :data:`STAGE_C_DEBT_ESCALATION_THRESHOLD`, the loop-breaker
    # prefers C over D even when a research corpus exists.
    # Default 0 keeps the field optional for existing call sites.
    stage_c_candidates_total: int = 0
    # Stage C drought fix — hours since last Stage C wake. Drives
    # Rule 2f (C floor): when this is at or above 8h AND
    # ``stage_c_candidates_total`` is at or above
    # :data:`STAGE_C_DEBT_ESCALATION_THRESHOLD`, schedule Stage C
    # even if the inbox has items. Mirrors the D-floor design (Rule
    # 2e). ``inf`` when no Stage C wake has been recorded; default
    # keeps the field optional for existing call sites.
    hours_since_last_c: float = float("inf")


@dataclass(frozen=True)
class PhaseConfig:
    """Per-phase tunables. Loaded from ``alice.config.json thinking.*``.

    Phase 3 of the migration ships ``enable_full_sleep_dispatch=True``
    as the new default — sleep wakes route to B/C/D from vault state.
    Set ``false`` in
    ``thinking.phase_routing.enable_full_sleep_dispatch`` to fall back
    to Phase-0 single-stage behavior if production behavior surprises.

    ``allowed_tools`` and ``max_seconds`` are config overrides over
    the runtime defaults: every non-Quick phase ships with the full
    tool set (:data:`alice_thinking.runtime._FULL_TOOL_ALLOWLIST`)
    and an unbounded budget (Quick keeps its 30s smoke-test guard).
    ``None`` / ``0`` mean "fall through to the runtime default."
    """

    quick_mode: bool = False
    enable_full_sleep_dispatch: bool = True

    # Budget override — 0 == fall through to the runtime default
    # (unbounded for real phases, 30s for Quick).
    max_seconds: int = 0
    allowed_tools: Optional[list[str]] = None

    # Stage D eligibility window
    recent_research_window_days: int = 7
    recent_research_min_count: int = 2

    # Cascade thresholds
    consecutive_b_threshold: int = 6
    consecutive_null_c_threshold: int = 6
    stage_d_nightly_cap: int = STAGE_D_NIGHTLY_CAP


# ---------------------------------------------------------------------------
# Vault snapshot construction
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Cheap YAML-ish parser for wake/note frontmatter."""
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


def _has_inbox_items(mind: pathlib.Path) -> bool:
    notes = mind / "inner" / "notes"
    if not notes.is_dir():
        return False
    try:
        for entry in os.scandir(notes):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.name.endswith((".md", ".markdown")):
                return True
    except OSError:
        return False
    return False


_PLACEHOLDER_PREFIXES = ("(empty", "(none", "*(empty", "*(none", "_(empty", "_(none")


def _open_section(text: str) -> str:
    """Return the body of the ``## Open`` section of an unresolved-style note.

    The file's authoring convention is ``## Open`` for the live backlog
    plus ad-hoc instructional prose elsewhere (frontmatter, tl;dr, usage
    notes). Probes that key off the whole file get tripped by that prose
    and fire false positives even when the live backlog is empty. The
    section helper scopes inspection to the live content.

    Returns ``""`` when ``## Open`` is absent.
    """
    lines = text.splitlines()
    in_open = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_open:
                break  # next H2 closes the Open section
            if stripped[3:].lower().lstrip().startswith("open"):
                in_open = True
            continue
        if in_open:
            out.append(line)
    return "\n".join(out).strip()


def _open_section_is_empty(section: str) -> bool:
    """Treat placeholder markers like ``*(empty — ...)`` as empty."""
    if not section:
        return True
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip italics / emphasis markers for the leading-token check.
        head = stripped.lstrip("*_ ").lstrip()
        if head.lower().startswith(_PLACEHOLDER_PREFIXES):
            continue
        return False
    return True


def _has_broken_links(mind: pathlib.Path) -> bool:
    """Cheap broken-link probe — `cortex-memory/unresolved.md` lists them.

    Scoped to the ``## Open`` section so frontmatter, tl;dr, and usage
    prose can't false-positive. A non-empty entry containing ``[[`` is
    evidence of an unresolved link.
    """
    p = mind / "cortex-memory" / "unresolved.md"
    if not p.is_file():
        return False
    try:
        section = _open_section(p.read_text())
    except OSError:
        return False
    if _open_section_is_empty(section):
        return False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head = stripped.lstrip("*_ ").lstrip()
        if head.lower().startswith(_PLACEHOLDER_PREFIXES):
            continue
        if "[[" in stripped:
            return True
    return False


def _has_orphan_stubs(mind: pathlib.Path) -> bool:
    """Probe for orphan stubs.

    A full O(n) vault scan is too expensive for every wake. Cheap proxy:
    inspect the ``## Open`` section of ``cortex-memory/unresolved.md``.
    The Phase 1 approximation kept the previous "any text in the file"
    check, but unresolved.md always has frontmatter + tl;dr + instructional
    prose, so the check fired every wake and pinned select_phase Rule 2a
    to ``Phase.SLEEP_B`` regardless of true vault state. Scope to the
    live backlog section instead.
    """
    p = mind / "cortex-memory" / "unresolved.md"
    if not p.is_file():
        return False
    try:
        section = _open_section(p.read_text())
    except OSError:
        return False
    return not _open_section_is_empty(section)


def _has_recent_research(
    mind: pathlib.Path, *, now: _dt.datetime, window_days: int, min_count: int
) -> bool:
    research = mind / "cortex-memory" / "research"
    if not research.is_dir():
        return False
    cutoff = (now - _dt.timedelta(days=window_days)).timestamp()
    fresh = 0
    try:
        for entry in research.rglob("*.md"):
            try:
                if entry.stat().st_mtime >= cutoff:
                    fresh += 1
                    if fresh >= min_count:
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _consecutive_stage_count(
    mind: pathlib.Path,
    *,
    stage: str,
    require_did_work_false: bool,
    now: _dt.datetime,
    window_hours: int = 24,
) -> int:
    """Count consecutive wake files matching ``stage`` from newest backwards.

    Single source of truth for the escalation counters: the wake-file
    history under ``inner/thoughts/<date>/`` already records stage:
    frontmatter on every wake, so deriving the streak from the live data
    is more reliable than maintaining a separate counter file. The prior
    implementation (``_read_counter``) read from counter files that no
    code path actually wrote, so the counters were always 0 and Rule 2b
    never fired.

    Delegates to :func:`alice_thinking.vault_state._consecutive_count` so
    both modules agree on the streak definition.
    """
    from . import vault_state as _vs

    thoughts_dir = mind / "inner" / "thoughts"
    if not thoughts_dir.is_dir():
        return 0
    since = now - _dt.timedelta(hours=window_hours)
    files = _vs._wake_files_within(thoughts_dir, since=since)
    return _vs._consecutive_count(
        files, stage=stage, require_did_work_false=require_did_work_false
    )


def _stage_d_cap_exhausted(state_dir: pathlib.Path, *, today: str, cap: int) -> bool:
    p = state_dir / f"stage-d-pairs-{today}.jsonl"
    if not p.is_file():
        return False
    syntheses = 0
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("synthesis") not in (None, ""):
                syntheses += 1
                if syntheses >= cap:
                    return True
    except OSError:
        return False
    return False


def _hours_since_last_d(
    state_dir: pathlib.Path,
    *,
    now: _dt.datetime,
) -> float:
    """Hours since the last Stage D wake. Returns infinity if no D wake exists.

    Reads ``stage-d-pairs-{date}.jsonl`` for today + yesterday (UTC dates
    of those files are typically aligned with local dates; we tolerate
    both by scanning two days back). A record counts as a completed D
    wake when it carries a non-empty ``synthesis`` field.

    Timestamps in the JSONL are UTC (``"...Z"``). ``now`` may be local
    (tz-aware EDT) or UTC; we normalize both ends to UTC before the
    delta to avoid the off-by-4-hours pitfall the design note flagged.
    """
    last_d_ts: Optional[_dt.datetime] = None

    for days_back in (0, 1):
        date_str = (now.date() - _dt.timedelta(days=days_back)).isoformat()
        pairs_file = state_dir / f"stage-d-pairs-{date_str}.jsonl"
        if not pairs_file.is_file():
            continue
        try:
            for line in pairs_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A record with a synthesis entry = a completed D wake.
                if rec.get("synthesis") not in (None, ""):
                    ts_str = rec.get("ts")
                    if not ts_str:
                        continue
                    try:
                        ts_dt = _dt.datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                    # Normalize to UTC for comparison.
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=_dt.timezone.utc)
                    else:
                        ts_dt = ts_dt.astimezone(_dt.timezone.utc)
                    if last_d_ts is None or ts_dt > last_d_ts:
                        last_d_ts = ts_dt
        except OSError:
            continue

    if last_d_ts is None:
        return float("inf")

    if now.tzinfo is None:
        now_utc = now.replace(tzinfo=_dt.timezone.utc)
    else:
        now_utc = now.astimezone(_dt.timezone.utc)
    delta = now_utc - last_d_ts
    return delta.total_seconds() / 3600


def _hours_since_last_c(
    mind: pathlib.Path,
    *,
    now: _dt.datetime,
    window_hours: int = 48,
) -> float:
    """Hours since the last Stage C wake. Returns infinity if none in window.

    Stage C, unlike Stage D, does not maintain its own pairs JSONL —
    its wake-file frontmatter is the source of truth. Walks
    ``inner/thoughts/<date>/`` newest-first looking for the most
    recent wake with ``stage: C``; returns the delta in hours.

    Used by Rule 2f (C floor) to break out of the Rule 2a (inbox
    drain) / Rule 2e (D floor) cascade lock that caused the Stage C
    drought (zero C wakes across 40 sleep-phase wakes 2026-05-08 →
    2026-05-27).

    ``window_hours`` bounds the scan — 48h is generous enough to
    cover a full sleep cycle (Rule 2f's threshold is 8h) plus
    headroom for clock drift, but cheap to walk.
    """
    from . import vault_state as _vs

    thoughts_dir = mind / "inner" / "thoughts"
    if not thoughts_dir.is_dir():
        return float("inf")

    since = now - _dt.timedelta(hours=window_hours)
    files = _vs._wake_files_within(thoughts_dir, since=since)
    if not files:
        return float("inf")

    if now.tzinfo is None:
        now_utc = now.replace(tzinfo=_dt.timezone.utc)
    else:
        now_utc = now.astimezone(_dt.timezone.utc)

    # Walk newest → oldest, return first Stage C wake's age in hours.
    for f in reversed(files):
        try:
            text = f.read_text()
        except OSError:
            continue
        fm = _vs._parse_frontmatter(text)
        if fm.get("stage", "").upper() != "C":
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        wake_dt = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
        delta = now_utc - wake_dt
        return delta.total_seconds() / 3600

    return float("inf")


def _vault_dir_mtime(mind: pathlib.Path) -> float:
    p = mind / "cortex-memory"
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _stage_c_candidates_total(mind: pathlib.Path) -> int:
    """Return ``stage_c_candidates.total`` for Rule 2b's debt gate.

    Issue #388: the loop-breaker needs to know how much Stage C work
    is queued (bloated notes + stale dailies). Delegates to
    :func:`metrics.vault_health.count_stage_c_candidates` so the
    selector and the morning vault-health snapshot agree on the
    same number. Lazy import keeps ``phase.py`` from taking a hard
    dependency on the metrics package at module load.

    Returns 0 when the vault is missing or the import / call fails —
    fails safe to the prior corpus-driven Rule 2b behavior.
    """
    vault_dir = mind / "cortex-memory"
    if not vault_dir.is_dir():
        return 0
    try:
        from metrics.vault_health import count_stage_c_candidates
    except ImportError:
        return 0
    try:
        result = count_stage_c_candidates(vault_dir)
    except OSError:
        return 0
    return int(result.get("total", 0))


def build_vault_snapshot(
    mind: pathlib.Path,
    *,
    now: _dt.datetime,
    state_dir: pathlib.Path,
    cfg: Optional[PhaseConfig] = None,
) -> VaultSnapshot:
    """Read the mind's current state into a :class:`VaultSnapshot`.

    Cheap I/O: a handful of stat calls + small file reads. Safe on a
    partial scaffold — every probe falls back to a sensible default
    when the underlying file is missing.
    """

    cfg = cfg or PhaseConfig()
    today = now.date().isoformat()

    return VaultSnapshot(
        hour=now.hour,
        minute=now.minute,
        has_inbox_items=_has_inbox_items(mind),
        has_broken_links=_has_broken_links(mind),
        has_orphan_stubs=_has_orphan_stubs(mind),
        has_recent_research=_has_recent_research(
            mind,
            now=now,
            window_days=cfg.recent_research_window_days,
            min_count=cfg.recent_research_min_count,
        ),
        consecutive_b=_consecutive_stage_count(
            mind, stage="B", require_did_work_false=True, now=now
        ),
        consecutive_null_c=_consecutive_stage_count(
            mind, stage="C", require_did_work_false=True, now=now
        ),
        stage_d_cap_exhausted=_stage_d_cap_exhausted(
            state_dir, today=today, cap=cfg.stage_d_nightly_cap
        ),
        hours_since_last_d=_hours_since_last_d(state_dir, now=now),
        vault_dir_mtime=_vault_dir_mtime(mind),
        state_dir=state_dir,
        today=today,
        stage_c_candidates_total=_stage_c_candidates_total(mind),
        hours_since_last_c=_hours_since_last_c(mind, now=now),
    )


# ---------------------------------------------------------------------------
# Phase selection
# ---------------------------------------------------------------------------


def select_phase(vault: VaultSnapshot, cfg: Optional[PhaseConfig] = None) -> Phase:
    """Pure deterministic phase selector.

    Phase 3 of the migration ships ``enable_full_sleep_dispatch=True``
    as the default — sleep wakes route through the full B/C/D cascade
    from vault state. Setting the flag to ``False`` (via
    ``alice.config.json``) restores Phase-0 single-stage behavior:
    every sleep wake collapses to :attr:`Phase.SLEEP_B`.
    """

    cfg = cfg or PhaseConfig()

    # Rule 0: smoke test always wins.
    if cfg.quick_mode:
        return Phase.QUICK

    # Rule 1: active window 07:00–22:59 local.
    if 7 <= vault.hour < 23:
        return Phase.ACTIVE

    # ---- Sleep window ----
    # Phase 0 short-circuit: behave like today (always B) until Phase 3
    # explicitly opts in to full sub-stage dispatch.
    if not cfg.enable_full_sleep_dispatch:
        return Phase.SLEEP_B

    # Rule 2f: Periodic C floor — every 8 hours without C, schedule C
    # even if the inbox has items. Checked BEFORE Rule 2e (D floor) so
    # that when both are ready (e.g. T=8h since last C, ≥4h since last D)
    # C gets the wake. Pre-swap ordering put 2e first, which meant D
    # always won at every T=8,12,16... boundary and C never fired — 92
    # sleep-phase wakes overnight 2026-05-27→28 produced zero C even
    # with 15 bloated notes signalling debt.
    #
    # Gated on ``stage_c_candidates_total`` so the floor only fires
    # when there is actual debt to drain — same threshold as Rule 2b
    # debt-weighted escalation (Issue #388). 8h cadence composes with
    # the 4h D floor below: C at T=8, D at next available 4h slot, etc.
    # Self-correcting — once C drains debt below the threshold, Rule 2f
    # stops firing and Rule 2a / Rule 2e resume.
    #
    # Design: cortex-memory/research/2026-05-27-stage-c-drought-analysis.md
    # Ordering fix: cortex-memory/research/2026-05-28-stage-c-floor-ordering-bug.md
    if (
        vault.hours_since_last_c >= 8
        and vault.stage_c_candidates_total >= STAGE_C_DEBT_ESCALATION_THRESHOLD
    ):
        return Phase.SLEEP_C

    # Rule 2e (promoted): Periodic D floor — every 4 hours without D, schedule D
    # even if the inbox has items. Previously Rule 2a (inbox drain) took priority
    # above this rule, but in practice cozylobe sensor notes keep the inbox
    # perpetually non-empty, which caused a 4-day Stage D drought (2026-05-22
    # → 2026-05-25). The 4-hour floor + has_recent_research + cap-not-exhausted
    # guardrails make promotion safe: at most one Stage D wake every 4 hours,
    # only when there is fresh material to recombine, and capped per night.
    if (
        vault.hours_since_last_d >= 4
        and vault.has_recent_research
        and not vault.stage_d_cap_exhausted
    ):
        return Phase.SLEEP_D

    # Rule 2a: inbox items / vault issues → Stage B (real work always wins
    # within the bounds of Rule 2e / 2f above).
    if vault.has_inbox_items or vault.has_broken_links or vault.has_orphan_stubs:
        return Phase.SLEEP_B

    # Rule 2b: 6+ consecutive Stage B wakes → break the loop.
    #
    # Issue #388: debt-weighted escalation. When
    # ``stage_c_candidates.total`` is at or above
    # :data:`STAGE_C_DEBT_ESCALATION_THRESHOLD`, prefer Stage C over
    # Stage D even with a research corpus. Without this gate, the
    # corpus-driven D-preference always won (corpus is non-empty every
    # night) and Stage C ran zero times across 40 sleep-phase wakes
    # post-Phase-3 deployment. Below the threshold, the original
    # corpus-driven Stage D preference is preserved. Self-correcting:
    # once C drains the candidates below 5, escalation reverts to D.
    if vault.consecutive_b >= cfg.consecutive_b_threshold:
        if vault.stage_c_candidates_total >= STAGE_C_DEBT_ESCALATION_THRESHOLD:
            return Phase.SLEEP_C
        if vault.has_recent_research and not vault.stage_d_cap_exhausted:
            return Phase.SLEEP_D
        return Phase.SLEEP_C

    # Rule 2c: early phase (23:00–02:59) → C (default sleep).
    if vault.hour in (23, 0, 1, 2):
        if (
            vault.consecutive_null_c >= cfg.consecutive_null_c_threshold
            and vault.has_recent_research
            and not vault.stage_d_cap_exhausted
        ):
            return Phase.SLEEP_D
        return Phase.SLEEP_C

    # Rule 2d: late phase (03:00–06:59) → D when corpus exists, else B.
    if vault.has_recent_research and not vault.stage_d_cap_exhausted:
        return Phase.SLEEP_D
    return Phase.SLEEP_B


# ---------------------------------------------------------------------------
# Prompt fragment loader
# ---------------------------------------------------------------------------


_PHASE_FRAGMENT_FILES: dict[Phase, str] = {
    Phase.ACTIVE: "active.md",
    Phase.SLEEP_B: "sleep-b.md",
    Phase.SLEEP_C: "sleep-c.md",
    Phase.SLEEP_D: "sleep-d.md",
    # QUICK is handled separately — it doesn't compose like a normal phase.
    # DESIGN_COMMISSION reuses the active fragment (same operational scaffolding;
    # the content being worked on is the commission spec itself).
    Phase.DESIGN_COMMISSION: "active.md",
    # REVISE is task-type dispatched, parallel to QUICK in shape: no tools,
    # fixed body. The reviser supplies the spec/draft/feedback as injected
    # content; the fragment instructs the model to return only revised draft.
    Phase.REVISE: "revise.md",
    # Per-issue phases — the spawn_thinking_agent entrypoint writes the
    # issue body (for DESIGN) or the approved design note (for BUILD)
    # into ``injected_content``; the fragment supplies the operating
    # instructions for that tier.
    Phase.PER_ISSUE_DESIGN: "per-issue-design.md",
    Phase.PER_ISSUE_BUILD: "per-issue-build.md",
}


# Phases that compose without the wake-mode prelude. The standard
# prelude is wake-centric (write a wake file, drain notes, the
# "research + memory only" constitutional boundary) and inappropriate
# for stimulus-spawned per-issue work — in particular, BUILD must
# touch code outside ``~/alice-mind/`` and open PRs, which the
# default prelude explicitly forbids. These phases ship their own
# framing in the fragment.
_PHASES_WITHOUT_PRELUDE: frozenset[Phase] = frozenset(
    {Phase.PER_ISSUE_DESIGN, Phase.PER_ISSUE_BUILD}
)


class PromptFragmentLoader:
    """Load and compose phase prompt fragments from package resources.

    The fragments live at ``alice_thinking/prompts/*.md``. Single source
    of truth; the repo is bind-mounted rw into workers, so edits take
    effect on the next wake. No vault override path.

    The :meth:`compose` method takes an optional ``injected_content``
    kwarg — currently unused, but plumbed for future STM/LTM injection
    (per §Required Interfaces for Companion Designs in the design doc).
    """

    PACKAGE = "alice_thinking.prompts"

    def __init__(self) -> None:
        # No mind_dir; fragments are package resources.
        pass

    def load_prelude(self) -> str:
        from importlib import resources

        return (resources.files(self.PACKAGE) / "prelude.md").read_text(
            encoding="utf-8"
        )

    def load_phase(self, phase: Phase) -> str:
        from importlib import resources

        if phase == Phase.QUICK:
            # Quick keeps its own minimal prompt — handled outside this loader.
            raise ValueError("Phase.QUICK is not composed via PromptFragmentLoader")
        try:
            filename = _PHASE_FRAGMENT_FILES[phase]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"no fragment for phase {phase!r}") from exc
        return (resources.files(self.PACKAGE) / filename).read_text(encoding="utf-8")

    def compose(
        self,
        phase: Phase,
        *,
        timestamp_header: str,
        injected_content: Optional[str] = None,
    ) -> str:
        """Compose the full prompt: ``timestamp_header + prelude + phase fragment``.

        ``injected_content`` is inserted between the prelude and the
        phase body. STM/LTM designs use it for substrate excerpts; the
        per-issue phases (:attr:`Phase.PER_ISSUE_DESIGN` /
        :attr:`Phase.PER_ISSUE_BUILD`) use it for the issue body or the
        approved design note that the entrypoint script reads from the
        spawn dir.

        Per-issue phases skip the wake-mode prelude
        (:data:`_PHASES_WITHOUT_PRELUDE`) — that prelude is wake-centric
        and its "no writes outside ``~/alice-mind/``" constitutional
        boundary is wrong for BUILD mode, which must open PRs and edit
        code under ``alice/``. The per-issue fragments carry their own
        framing.
        """
        phase_body = self.load_phase(phase)
        sections: list[str] = [timestamp_header.rstrip()]
        if phase not in _PHASES_WITHOUT_PRELUDE:
            prelude = self.load_prelude()
            sections += ["", prelude.rstrip()]
        if injected_content:
            sections += ["", "---", "", injected_content.rstrip()]
        sections += ["", "---", "", phase_body.rstrip(), ""]
        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Design commission task detection
# ---------------------------------------------------------------------------


def detect_commission_notes(mind: pathlib.Path) -> list[pathlib.Path]:
    """Return commission notes sorted oldest-first (by mtime).

    A note qualifies as a design-commission task if any of:

    1. Its frontmatter contains ``task_type: design-commission``.
    2. Its filename matches ``*design-commission*.md``.
    3. It lives under ``inner/notes/.design-commissions/``.

    Hidden (``.``-prefixed) files outside the dedicated folder are
    ignored to match the inbox-drain semantics.
    """

    notes_dir = mind / "inner" / "notes"
    out: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()

    def _push(p: pathlib.Path) -> None:
        if p in seen:
            return
        out.append(p)
        seen.add(p)

    if notes_dir.is_dir():
        for f in notes_dir.glob("*.md"):
            if f.name.startswith("."):
                continue
            try:
                fm = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if (
                fm.get("task_type", "").strip().strip('"').strip("'")
                == "design-commission"
            ):
                _push(f)

        # Filename fallback
        for f in notes_dir.glob("*design-commission*.md"):
            if f.name.startswith("."):
                continue
            _push(f)

    # Folder fallback
    commission_dir = notes_dir / ".design-commissions"
    if commission_dir.is_dir():
        for f in commission_dir.glob("*.md"):
            _push(f)

    out.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0.0)
    return out


# ---------------------------------------------------------------------------
# Conflict resolution task detection
# ---------------------------------------------------------------------------


# After this many consecutive ``deferred`` verdicts on the same conflict
# note, :func:`record_conflict_deferral` flips ``status`` to ``stale`` so
# :func:`detect_conflict_notes` drops it from the queue. Real resolution
# logic ships separately (see ``wake.py``'s ``_run_conflict_resolution``
# docstring); this is a guardrail against the stub eating every wake.
# Five wakes is enough to surface the conflict in telemetry without
# burning hours of cadence on a no-op preempt (issue #203).
CONFLICT_DEFER_THRESHOLD = 5


def detect_conflict_notes(vault_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return open conflict notes from ``cortex-memory/conflicts/``.

    A conflict note counts as "open" when its frontmatter ``status``
    field is ``"open"`` OR the field is absent (treat absent as
    open — vault contradictions land here unannotated).

    Files under ``cortex-memory/conflicts/.resolved/`` are excluded
    (resolved archive). Top-level hidden files are also ignored.

    Output is sorted oldest-first by mtime (same convention as
    :func:`detect_commission_notes`).

    ``vault_dir`` is the vault root (``cortex-memory/``). Pass
    ``mind / "cortex-memory"`` from callers in ``wake.py``.
    """

    conflicts_dir = vault_dir / "conflicts"
    if not conflicts_dir.is_dir():
        return []

    out: list[pathlib.Path] = []
    for f in conflicts_dir.glob("*.md"):
        if f.name.startswith("."):
            continue
        if not f.is_file():
            continue
        try:
            fm = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        status = fm.get("status", "").strip().strip('"').strip("'").lower()
        if status and status != "open":
            continue
        out.append(f)

    out.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0.0)
    return out


def _write_frontmatter_fields(text: str, *, updates: dict[str, str]) -> str:
    """Rewrite frontmatter with the supplied key/value updates.

    Existing keys are replaced in-line (line position preserved); new
    keys are appended at the end of the frontmatter block. If the file
    has no frontmatter, a fresh block is prepended.

    Pairs with :func:`_parse_frontmatter` — the writer doesn't preserve
    YAML quoting/anchors because the parser doesn't either. Sufficient
    for the small set of fields thinking owns on conflict notes.
    """

    m = _FRONTMATTER_RE.match(text)
    if not m:
        header = ["---"] + [f"{k}: {v}" for k, v in updates.items()] + ["---", ""]
        return "\n".join(header) + text

    body = text[m.end():]
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            out_lines.append(line)
            continue
        key, _, _ = stripped.partition(":")
        key = key.strip()
        if key in updates and key not in seen:
            out_lines.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            out_lines.append(f"{k}: {v}")
    return "---\n" + "\n".join(out_lines) + "\n---\n" + body


def record_conflict_deferral(
    conflict_note: pathlib.Path,
    *,
    threshold: int = CONFLICT_DEFER_THRESHOLD,
) -> tuple[int, bool]:
    """Bump ``defer_count`` on a conflict note; flip to ``stale`` at threshold.

    Wake-eating mitigation for issue #203. The conflict-resolution
    preempt in :mod:`alice_thinking.wake` is a stub that always returns
    ``verdict="deferred"`` and today never updates the note. Without a
    counter, every subsequent wake re-detects the same open conflict,
    defers, and exits — an infinite loop until something external marks
    the file resolved.

    Each call:

    - Reads ``defer_count`` from the note's frontmatter (defaulting to
      ``0`` if absent or malformed) and bumps it by one.
    - When the new count reaches ``threshold``, flips ``status`` to
      ``stale``. :func:`detect_conflict_notes` only returns notes whose
      status is ``open`` (or absent), so the stale note drops out of
      the queue and the wake proceeds to its normal body next tick.
    - Rewrites the file in place, preserving the body and any other
      frontmatter fields.

    Returns ``(new_count, marked_stale)``. Returns ``(0, False)`` when
    the file can't be read.
    """

    try:
        text = conflict_note.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, False

    fm = _parse_frontmatter(text)
    raw_prev = fm.get("defer_count", "0").strip().strip('"').strip("'")
    try:
        prev = int(raw_prev) if raw_prev else 0
    except ValueError:
        prev = 0

    new_count = prev + 1
    marked_stale = new_count >= threshold

    updates: dict[str, str] = {"defer_count": str(new_count)}
    if marked_stale:
        updates["status"] = "stale"

    new_text = _write_frontmatter_fields(text, updates=updates)
    try:
        conflict_note.write_text(new_text, encoding="utf-8")
    except OSError:
        pass

    return new_count, marked_stale
