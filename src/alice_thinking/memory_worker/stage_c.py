"""Stage C — vault grooming.

Deterministic, model-free maintenance of ``cortex-memory/``. Each
tick performs up to four operations in order:

1. **atomize**     — split notes longer than :data:`BLOATED_LINE_THRESHOLD`
   lines on top-level ``## `` heading boundaries.
2. **archive**     — move dailies older than :data:`STALE_DAILY_DAYS`
   into ``archive/dailies/<year>/``.
3. **dedupe-merge** — collapse pairs of notes that share a slug or whose
   titles are within :data:`FUZZY_TITLE_DISTANCE` Levenshtein edits.
4. **orphan-resolve** — auto-link zero-inbound notes to a single matching
   parent (by frontmatter ``tags:``); queue ambiguous ones to
   ``inner/orphans-pending.md`` for human/thinking review.

Design contract — see
``cortex-memory/research/2026-06-01-memory-worker-extraction-design.md``
§ "Phase 3: Stage C Grooming — Design Input (2026-06-02)" for the
UNION trigger rationale and incremental-cap design.

NO LLM CALLS. All four operations are mechanical and reversible from
the journal. Per-op verifiers (registered with the journal module)
back replay on crash recovery.

Trigger
-------

Stage C runs when ANY of the following is true (UNION, not AND):

  * ``bloated_notes > 0``                   — structural debt
  * ``stale_dailies > 0``                   — structural debt
  * ``decayed_notes_in_window > threshold`` — decay backlog (NEW)
  * ``orphans > 0``                         — graph islands
  * ``broken_wikilinks > 0``                — graph debt

This is the fix for the 82-sleep Stage C drought: the old structural
trigger missed the ~570-note decay backlog accumulating in the vault.

Incremental cap
---------------

Each operation processes at most :data:`DEFAULT_MAX_ITEMS_PER_CATEGORY`
items per tick. The cap keeps wake budget bounded; a 600-item backlog
drains over the next dozen wakes instead of dominating one.

Path conventions
----------------

Like :mod:`stage_b`, all paths resolve relative to the *mind root*
(``~/alice-mind``). The vault lives at ``<mind>/cortex-memory/`` and
the inbox at ``<mind>/inner/notes/``. Helpers from
:mod:`metrics.vault_health` operate on the vault root directly — we
pass ``mind / "cortex-memory"`` when calling into them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import logging
import pathlib
import re
import time
from collections import defaultdict
from typing import Any, Optional

from indexer.yaml_lite import _strip_code, split_frontmatter

from alice_thinking import vault_lock

from . import journal as journal_mod
from . import correction_cascade as cascade_mod


logger = logging.getLogger(__name__)


#: Vault-lock timeout (seconds) for every Stage C mutation. Stage C
#: writes are short — if a holder is sitting on the sidecar for more
#: than this, the lock acquirer raises :class:`vault_lock.VaultLockTimeout`
#: and the per-op loop logs + skips the file. The next cadence tick
#: retries.
#:
#: Tuned to be a little longer than a typical Stage B inbox-drain
#: write (~200 ms) but well under the 30-min cadence so a hung peer
#: doesn't wedge a whole tick.
_LOCK_TIMEOUT_SECONDS = 5.0


# ---------- tunables ----------

#: Default decay-backlog threshold above which Stage C is allowed to
#: run for decay reasons alone. Configurable via
#: ``memory_worker.stage_c.decay_threshold`` in ``alice.config.json``.
DEFAULT_DECAY_THRESHOLD = 50

#: Default per-category processing cap per cycle. Configurable via
#: ``memory_worker.stage_c.max_items_per_category``.
DEFAULT_MAX_ITEMS_PER_CATEGORY = 20

#: A note above this length (in lines) is an atomize candidate. Matches
#: :func:`metrics.vault_health.count_stage_c_candidates`'s default.
BLOATED_LINE_THRESHOLD = 250

#: Daily older than this many days is an archive candidate.
STALE_DAILY_DAYS = 90

#: A note with ``status: open`` and zero access count older than this
#: many days is a stale-open archive candidate. See the 2026-06-06
#: validation sample (19/20 confirmed genuinely abandoned).
STALE_OPEN_AGE_DAYS = 30

#: Default for :class:`StageCConfig.archive_stale_open_dry_run`. Ships
#: dry-run so the first production run logs the ~69 expected candidates
#: without modifying the vault — Speaking reviews the log before
#: flipping to apply. Flip via
#: ``memory_worker.stage_c.archive_stale_open_dry_run`` in
#: ``alice.config.json``.
DEFAULT_ARCHIVE_STALE_OPEN_DRY_RUN = True

#: Default for :class:`StageCConfig.atomize_disambiguate_on_collision`.
#: When a proposed child slug already exists somewhere in the vault,
#: ships SKIP-on-collision (False): the colliding section is left
#: inline in the parent and a warning is logged. Flip to True to
#: instead generate disambiguated slugs (``{slug}-2``, ``{slug}-3``,
#: …) and force-create the child.
#:
#: Default is SKIP because a cross-vault slug collision is usually a
#: signal that the source was already atomized in a previous run (or
#: by an older path) and re-atomizing would manufacture a *shadow
#: orphan* — two notes with the same slug, where wikilinks only
#: resolve to one. See the 2026-06-07 slug-dedup-recurrence analysis.
#: Flip via ``memory_worker.stage_c.atomize_disambiguate_on_collision``
#: in ``alice.config.json``.
DEFAULT_ATOMIZE_DISAMBIGUATE_ON_COLLISION = False

#: Fuzzy title-match distance for dedupe. A pair of notes whose titles
#: are within this Levenshtein distance (case-insensitive) is treated as
#: a duplicate group. Slug-equality is checked independently and always
#: dedupes regardless of distance.
FUZZY_TITLE_DISTANCE = 3

#: Decay window in days. Matches the design's "7-day window" callout.
DECAY_WINDOW_DAYS = 7

#: Folders within ``cortex-memory/`` that are excluded from grooming
#: candidate sets. Phase 2 expansion (cortex-memory/research/
#: 2026-06-10-decay-phase-2-implementation-spec.md): ``decisions/`` and
#: ``feedback/`` are read-once-referenced — intentionally low-access by
#: design — and shouldn't be groomed for decay. Same as
#: :data:`alice_thinking.memory_worker.stage_d._DECAY_EXCLUDED_TOP_DIRS`
#: so the two passes see the same population.
_EXCLUDED_TOP_DIRS = frozenset(
    {"dailies", "archive", "gh-state", "decisions", "feedback"}
)

#: Files that aren't real notes — mirrors
#: :data:`metrics.vault_health.EXCLUDED_NAMES`.
_EXCLUDED_NAMES = frozenset({"index.md", "README.md", "unresolved.md"})


# ---------- reports + state ----------


@dataclasses.dataclass
class StageCState:
    """Trigger inputs computed before deciding whether to run."""

    bloated_notes: int = 0
    stale_dailies: int = 0
    decayed_notes_in_window: int = 0
    orphans: int = 0
    broken_wikilinks: int = 0


@dataclasses.dataclass
class StageCReport:
    """Counts of mutations performed this tick."""

    atomize: int = 0
    archive: int = 0
    archive_stale_open: int = 0
    dedupe_merge: int = 0
    orphan_resolve: int = 0
    correction_pairs_checked: int = 0
    unpropagated_corrections: int = 0
    ran: bool = False

    def to_dict(self) -> dict[str, int]:
        # Mypy/json-friendly: only return the int fields the heartbeat
        # cares about. ``ran`` is exposed separately via :func:`run`.
        return {
            "atomize": self.atomize,
            "archive": self.archive,
            "archive_stale_open": self.archive_stale_open,
            "dedupe_merge": self.dedupe_merge,
            "orphan_resolve": self.orphan_resolve,
            "correction_pairs_checked": self.correction_pairs_checked,
            "unpropagated_corrections": self.unpropagated_corrections,
        }


# ---------- path helpers ----------


def _vault_dir(mind: pathlib.Path) -> pathlib.Path:
    return mind / "cortex-memory"


def _archive_dailies_dir(mind: pathlib.Path) -> pathlib.Path:
    return _vault_dir(mind) / "archive" / "dailies"


def _orphans_pending_path(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "orphans-pending.md"


def _events_jsonl_path(mind: pathlib.Path) -> pathlib.Path:
    return mind / "memory" / "events.jsonl"


def _is_groomable(rel_parts: tuple[str, ...], name: str) -> bool:
    """True if a note under ``cortex-memory/<rel_parts>`` is a
    candidate for atomize / dedupe / orphan operations.

    Mirrors the exclusion rules used by
    :func:`metrics.vault_health.count_stage_c_candidates` — top-level
    dailies/archive/gh-state are off-limits; scaffolding files
    (``index.md`` etc.) are skipped. Hidden directories (``.``) are
    skipped too so ``.obsidian/`` doesn't bleed in.
    """
    if not rel_parts:
        return False
    if rel_parts[0] in _EXCLUDED_TOP_DIRS:
        return False
    if any(part.startswith(".") for part in rel_parts):
        return False
    if name in _EXCLUDED_NAMES:
        return False
    return True


def _iter_groomable_notes(vault: pathlib.Path) -> list[pathlib.Path]:
    """All ``cortex-memory/*.md`` paths eligible for grooming.

    Deterministic order (sorted) so a partial cycle is reproducible.
    """
    if not vault.is_dir():
        return []
    out: list[pathlib.Path] = []
    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if not _is_groomable(rel_parts, md.name):
            continue
        out.append(md)
    out.sort()
    return out


def _today() -> datetime.date:
    return datetime.date.today()


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- vault state collection ----------


def _line_count(path: pathlib.Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _count_bloated(vault: pathlib.Path) -> int:
    n = 0
    for md in _iter_groomable_notes(vault):
        if _line_count(md) > BLOATED_LINE_THRESHOLD:
            n += 1
    return n


def _count_stale_dailies(vault: pathlib.Path, today: datetime.date) -> int:
    dailies = vault / "dailies"
    if not dailies.is_dir():
        return 0
    cutoff = today - datetime.timedelta(days=STALE_DAILY_DAYS)
    cutoff_str = cutoff.isoformat()
    n = 0
    for md in dailies.glob("*.md"):
        stem = md.stem
        if len(stem) >= 10 and stem[:10] < cutoff_str:
            n += 1
    return n


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _extract_wikilink_targets(body: str) -> list[str]:
    """Pull every ``[[target]]`` (or aliased / anchored variant)
    target slug out of ``body``. Strips fenced code blocks and inline
    code spans via ``_strip_code`` to suppress false positives from
    bash ``[[ -d ]]`` tests, markdown examples, and backtick spans."""
    cleaned = _strip_code(body)
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(cleaned)]


def _slug_of(md: pathlib.Path, vault: pathlib.Path) -> str:
    """Slug used by wikilinks. The vault convention is filename stem;
    explicit ``slug:`` frontmatter overrides if present."""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return md.stem
    fm, _body = split_frontmatter(text)
    raw = fm.get("slug")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return md.stem


def _build_inbound_index(vault: pathlib.Path) -> dict[str, set[str]]:
    """Map ``slug -> {source-rel-paths}`` — every note that links to it.

    Source paths are relative to ``vault``. The slug key is the bare
    filename stem of the target (no folder); wikilinks address by
    basename in this vault.
    """
    inbound: dict[str, set[str]] = defaultdict(set)
    if not vault.is_dir():
        return inbound
    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        _fm, body = split_frontmatter(text)
        rel = str(md.relative_to(vault))
        for target in _extract_wikilink_targets(body):
            # Normalize: strip folder prefix and lowercase. We track
            # both forms so callers can look up either.
            base = target.rsplit("/", 1)[-1]
            inbound[base].add(rel)
            inbound[base.lower()].add(rel)
    return inbound


def _count_orphans(vault: pathlib.Path) -> int:
    """Notes with zero inbound wikilinks, excluding dailies/archive/gh-state.

    This is a Stage C-specific orphan count (matching what
    :func:`orphan_resolve` will iterate over) — we don't use
    :func:`metrics.vault_health.count_orphans` because that one also
    considers aliases, which inflates the count above what Stage C can
    actually resolve via tags."""
    inbound = _build_inbound_index(vault)
    n = 0
    for md in _iter_groomable_notes(vault):
        if md.stem in inbound or md.stem.lower() in inbound:
            continue
        n += 1
    return n


def _count_broken_wikilinks(vault: pathlib.Path) -> int:
    """Wikilink target slugs that don't resolve to any vault file.

    Slug = basename. We resolve case-insensitively and only count
    targets coming from groomable source files — broken links inside
    archived dailies aren't Stage C's problem."""
    if not vault.is_dir():
        return 0
    by_slug: set[str] = set()
    for md in vault.rglob("*.md"):
        by_slug.add(md.stem.lower())
    broken = 0
    for md in _iter_groomable_notes(vault):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        _fm, body = split_frontmatter(text)
        for target in _extract_wikilink_targets(body):
            base = target.rsplit("/", 1)[-1].lower()
            if base and base not in by_slug:
                broken += 1
    return broken


def _count_decayed_in_window(vault: pathlib.Path, today: datetime.date) -> int:
    """Phase 2: count groomable notes with continuous decay score
    ``D >= 0.20`` (cortex-memory/research/2026-06-10-decay-phase-2-
    implementation-spec.md).

    Replaces the binary ``last_accessed > 7d AND access_count <= 1``
    check with the continuous scoring formula from
    :func:`metrics.vault_health.compute_decay_score`. Phase 2.5 all-
    links exemption is applied for structurally-isolated notes that
    would otherwise be flagged.

    Excludes ``dailies/``, ``archive/``, ``gh-state/``, ``decisions/``,
    ``feedback/`` per :data:`_EXCLUDED_TOP_DIRS`. Returns an ``int`` so
    the Stage C trigger logic (``state.decayed_notes_in_window >
    config.decay_threshold``) is unchanged.
    """
    # Lazy import — :mod:`metrics.vault_health` imports
    # :func:`alice_thinking.memory_worker.stage_d._is_fitness_domain`
    # at module load. Importing :mod:`metrics.vault_health` at the top
    # of this file would form an import cycle through stage_d.
    from metrics.vault_health import (
        DEFAULT_DECAY_SCORE_THRESHOLD,
        compute_all_links_degree,
        compute_decay_score,
    )
    from alice_thinking.memory_worker.stage_d import _phase_2_5_exempt

    if not vault.is_dir():
        return 0

    # Build structural inbound index once for the whole pass.
    inbound: dict[str, set[str]] = defaultdict(set)
    for md in _iter_groomable_notes(vault):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        _fm, body = split_frontmatter(text)
        source_rel = str(md.relative_to(vault))
        for target in _extract_wikilink_targets(body):
            base = target.rsplit("/", 1)[-1]
            if base:
                inbound[base.lower()].add(source_rel)

    n = 0
    for md in _iter_groomable_notes(vault):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        in_deg = len(inbound.get(md.stem.lower(), set()))
        score = compute_decay_score(
            fm=fm,
            body=body,
            in_degree=in_deg,
            vault_dir=vault,
            note_path=md,
            today=today,
        )
        if score < DEFAULT_DECAY_SCORE_THRESHOLD:
            continue
        # Phase 2.5: pay the all-links walk only on notes that would
        # otherwise be counted. Keeps the worst-case bounded; on a typical
        # vault only ~12% of notes trip the threshold.
        if in_deg < 10:
            all_deg = compute_all_links_degree(vault, md)
            if _phase_2_5_exempt(in_deg, all_deg):
                continue
        n += 1
    return n


def compute_state(mind: pathlib.Path) -> StageCState:
    """Snapshot the five trigger signals."""
    vault = _vault_dir(mind)
    today = _today()
    return StageCState(
        bloated_notes=_count_bloated(vault),
        stale_dailies=_count_stale_dailies(vault, today),
        decayed_notes_in_window=_count_decayed_in_window(vault, today),
        orphans=_count_orphans(vault),
        broken_wikilinks=_count_broken_wikilinks(vault),
    )


@dataclasses.dataclass
class StageCConfig:
    decay_threshold: int = DEFAULT_DECAY_THRESHOLD
    max_items_per_category: int = DEFAULT_MAX_ITEMS_PER_CATEGORY
    #: Dry-run gate for :func:`archive_stale_open`. Ships True; flip to
    #: False to enable real archival after Speaking reviews the dry-run
    #: log of candidate slugs.
    archive_stale_open_dry_run: bool = DEFAULT_ARCHIVE_STALE_OPEN_DRY_RUN
    #: Behavior when :func:`atomize` would create a child note whose
    #: slug already exists anywhere in the vault. False (default) =
    #: SKIP that child, preserve its section inline in the parent,
    #: log a warning. True = generate a disambiguated slug
    #: (``{slug}-2``, etc.) and force-create the child.
    atomize_disambiguate_on_collision: bool = (
        DEFAULT_ATOMIZE_DISAMBIGUATE_ON_COLLISION
    )


def should_run_c(state: StageCState, config: StageCConfig) -> bool:
    """UNION trigger: any single non-zero signal is enough."""
    return (
        state.bloated_notes > 0
        or state.stale_dailies > 0
        or state.decayed_notes_in_window > config.decay_threshold
        or state.orphans > 0
        or state.broken_wikilinks > 0
    )


# ---------- atomize ----------


#: Fitness domain notes are fixed-schedule skill-path writes, not
#: behavioral decay. Exempt from the decay-priority boost so a weekly
#: workout log doesn't get atomized just for sitting at access_count=0.
FITNESS_TAGS = frozenset({"fitness", "workout", "nutrition", "weight"})

#: Cap on the age component of :func:`_decay_priority_score`. A 6-month
#: old decayed note maxes out the age bonus; older notes don't keep
#: climbing.
_DECAY_AGE_CAP = 0.5

#: Atomize threshold relaxation for decay-boosted notes. A decayed note
#: at 0.8 × :data:`BLOATED_LINE_THRESHOLD` lines is still a viable
#: atomization target — recovering its content into hub sections is
#: usually worth a shorter parent. Non-decayed notes still need the
#: full threshold to qualify.
_DECAY_ATOMIZE_FRACTION = 0.8


def _decay_priority_score(
    path: pathlib.Path,
    *,
    vault: pathlib.Path | None = None,
) -> float:
    """Phase 2: priority boost for a decayed note in :func:`atomize`
    selection. Returns the continuous decay score from
    :func:`metrics.vault_health.compute_decay_score`, scaled to
    ``[0.0, 10.0]`` so the existing sort key (decay-boosted notes
    first, regular bloated notes second) still works.

    Returns ``0.0`` for fitness-domain notes (fixed-schedule writes,
    not behavioral decay — see :data:`FITNESS_TAGS`). Also returns 0.0
    when the score is below the decay threshold, so the atomize
    selection only boosts notes that are actually decayed.

    Phase 2.5 (all-links exemption) is applied via
    :func:`alice_thinking.memory_worker.stage_d._phase_2_5_exempt`:
    well-connected isolated notes get no boost.

    ``vault`` may be passed for accurate ``in_degree`` lookup; without
    it the in_degree defaults to 0 (worst-case for the candidate, but
    folder_resistance still dominates the formula for stable folders).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0.0
    fm, body = split_frontmatter(text)
    tags = set(_tags_of(fm))
    if tags & FITNESS_TAGS:
        return 0.0

    # Lazy import — see _count_decayed_in_window for cycle rationale.
    from metrics.vault_health import (
        DEFAULT_DECAY_SCORE_THRESHOLD,
        compute_all_links_degree,
        compute_decay_score,
    )
    from alice_thinking.memory_worker.stage_d import _phase_2_5_exempt

    # in_degree: 0 unless ``vault`` is supplied; even with vault we keep
    # the lookup cheap (one walk over groomable notes).
    in_deg = 0
    if vault is not None and vault.is_dir():
        stem_key = path.stem.lower()
        for md in _iter_groomable_notes(vault):
            try:
                if md.resolve() == path.resolve():
                    continue
            except OSError:
                if md == path:
                    continue
            try:
                t = md.read_text(encoding="utf-8")
            except OSError:
                continue
            _fm, b = split_frontmatter(t)
            for target in _extract_wikilink_targets(b):
                base = target.rsplit("/", 1)[-1].lower()
                if base == stem_key:
                    in_deg += 1
                    break

    score = compute_decay_score(
        fm=fm,
        body=body,
        in_degree=in_deg,
        vault_dir=vault if vault is not None else path.parent,
        note_path=path,
        today=_today(),
    )
    if score < DEFAULT_DECAY_SCORE_THRESHOLD:
        return 0.0

    # Phase 2.5: zero out if the all-links exemption applies.
    if vault is not None and vault.is_dir() and in_deg < 10:
        all_deg = compute_all_links_degree(vault, path)
        if _phase_2_5_exempt(in_deg, all_deg):
            return 0.0

    # Scale [0.0, 1.0] → [0.0, 10.0] so the boost slots above non-decay
    # bloated candidates (which score 0) in the atomize sort.
    return round(score * 10.0, 4)


def _split_on_headings(body: str) -> list[tuple[str, str]]:
    """Split ``body`` on top-level ``## `` headings.

    Returns a list of ``(heading_title, section_text)`` pairs. The
    pre-heading prologue (if any) is dropped — it stays in the parent.
    ``heading_title`` is the heading line with ``## `` stripped and
    whitespace normalized; ``section_text`` includes the heading line
    itself so the child note preserves its top-level header.
    """
    lines = body.splitlines(keepends=False)
    sections: list[tuple[str, str]] = []
    cur_title: Optional[str] = None
    cur_lines: list[str] = []
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            if cur_title is not None:
                sections.append((cur_title, "\n".join(cur_lines)))
            cur_title = line[3:].strip()
            cur_lines = [line]
        else:
            if cur_title is not None:
                cur_lines.append(line)
    if cur_title is not None:
        sections.append((cur_title, "\n".join(cur_lines)))
    return sections


def _slugify(title: str) -> str:
    """Turn a heading title into a filesystem-safe slug suffix.

    Lowercase, ASCII alphanumerics + hyphens; runs of non-alphanumerics
    collapse to a single hyphen.
    """
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "section"


def _build_vault_slug_index(vault: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map of ``slug.lower() -> first .md path found`` across the whole
    vault. Used by :func:`atomize` as a pre-flight against shadow-orphan
    duplicates — wikilinks resolve by slug regardless of folder, so a
    new note that re-uses an existing slug becomes unreachable.

    Includes ``dailies/``, ``archive/``, and ``gh-state/`` (slug-
    resolution doesn't respect groomability), but is otherwise
    best-effort: if the same stem appears twice (which is exactly the
    bug class this guards against), only the first wins. The caller
    treats this index as mutable — newly-written children should be
    added so subsequent sub-notes in the same atomize pass see them.
    """
    out: dict[str, pathlib.Path] = {}
    if not vault.is_dir():
        return out
    for md in vault.rglob("*.md"):
        key = md.stem.lower()
        if key not in out:
            out[key] = md
    return out


def vault_has_slug(vault: pathlib.Path, slug: str) -> bool:
    """Best-effort check: does any ``.md`` file under ``vault`` have a
    stem that matches ``slug`` case-insensitively?

    Used by :func:`atomize` as a pre-flight to detect cross-vault slug
    collisions before creating new child notes. The check is
    O(vault) — for hot paths that need many lookups, build a slug
    index once with :func:`_build_vault_slug_index` instead.
    """
    if not vault.is_dir():
        return False
    target = slug.lower()
    for md in vault.rglob("*.md"):
        if md.stem.lower() == target:
            return True
    return False


def _frontmatter_render(fm: dict[str, Any]) -> str:
    """Render a frontmatter dict back into YAML-ish lines.

    Same flat key/value shape :mod:`indexer.yaml_lite` parses. Order:
    the keys we care about most come first (title/tags/created/updated),
    then everything else in insertion order. We intentionally don't
    re-parse the original YAML — Stage C only writes flat keys.
    """
    preferred = ["title", "tags", "created", "updated", "last_accessed", "access_count"]
    out: list[str] = ["---"]
    seen: set[str] = set()
    for key in preferred:
        if key in fm:
            out.append(_render_kv(key, fm[key]))
            seen.add(key)
    for key, val in fm.items():
        if key in seen:
            continue
        out.append(_render_kv(key, val))
    out.append("---")
    return "\n".join(out) + "\n"


def _render_kv(key: str, val: Any) -> str:
    if isinstance(val, list):
        # Flow-style list for tag-like fields. Stringify each item.
        items = ", ".join(str(v) for v in val)
        return f"{key}: [{items}]"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    return f"{key}: {val}"


def atomize(
    mind: pathlib.Path,
    max_items: int,
    journal_path: Optional[pathlib.Path],
    *,
    disambiguate_on_collision: bool = DEFAULT_ATOMIZE_DISAMBIGUATE_ON_COLLISION,
) -> int:
    """Split bloated notes on top-level ``## `` boundaries.

    Returns the count of parent notes atomized this cycle.

    ``disambiguate_on_collision`` controls behavior when a proposed
    child slug already exists somewhere in the vault — see
    :data:`DEFAULT_ATOMIZE_DISAMBIGUATE_ON_COLLISION` for the
    rationale behind the SKIP-by-default policy.
    """
    vault = _vault_dir(mind)
    today_iso = _today().isoformat()
    processed = 0

    # Vault-wide slug index built once per atomize() call. We mutate
    # it as new children are written so two source notes processed in
    # the same tick can't collide with each other's freshly-written
    # children. (Within one source note the existing same-dir clobber
    # loop already serializes sibling sections.)
    slug_index = _build_vault_slug_index(vault)

    # Phase 3: decay-priority ordering. Zero-access notes get a boost
    # so they sort ahead of regular bloated notes within one tick's
    # cap, and they qualify at a relaxed line-count threshold (the
    # feedback-loop bet: surfacing decayed content as standalone notes
    # is worth atomizing slightly-shorter parents).
    candidates = _iter_groomable_notes(vault)
    scored = [(md, _decay_priority_score(md, vault=vault)) for md in candidates]
    scored.sort(key=lambda t: (-t[1], str(t[0])))

    for md, decay_boost in scored:
        if processed >= max_items:
            break
        threshold = (
            BLOATED_LINE_THRESHOLD * _DECAY_ATOMIZE_FRACTION
            if decay_boost > 0
            else BLOATED_LINE_THRESHOLD
        )
        if _line_count(md) <= threshold:
            continue

        # Hold the source EXCLUSIVE lock from read through parent
        # rewrite. This is the structural fix for the
        # "thinking-appends-while-we're-splitting" race the design
        # doc §6 calls out: while we hold the sidecar, thinking
        # blocks; by the time it gets the lock, the source has been
        # rewritten to the pointer-only stub. Lock ordering is
        # source-first then children-in-path-order, both alphabetical
        # against any sibling locker, so no deadlock with another
        # atomize-style operation hitting the same directory.
        #
        # The :func:`vault_lock.acquire` call is a ``@contextmanager``,
        # so the timeout fires on ``__enter__`` (entering ``with``),
        # not on the call itself — the try/except wraps the ``with``.
        try:
            with vault_lock.acquire(
                md,
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ):
                if _atomize_one(
                    md,
                    vault=vault,
                    today_iso=today_iso,
                    journal_path=journal_path,
                    slug_index=slug_index,
                    disambiguate_on_collision=disambiguate_on_collision,
                ):
                    processed += 1
        except vault_lock.VaultLockTimeout as exc:
            logger.warning("stage_c atomize: lock timeout for %s: %s", md, exc)
            continue
    return processed


def _atomize_one(
    md: pathlib.Path,
    *,
    vault: pathlib.Path,
    today_iso: str,
    journal_path: Optional[pathlib.Path],
    slug_index: dict[str, pathlib.Path],
    disambiguate_on_collision: bool,
) -> bool:
    """Atomize a single note. Caller holds the EXCLUSIVE lock on
    ``md``. Returns True on a successful split, False on read failure
    / no-headings / write failure / all-sections-collided.

    Extracted so the try/except VaultLockTimeout in :func:`atomize`
    cleanly wraps the ``with vault_lock.acquire(...)`` without
    duplicating the per-note body.

    ``slug_index`` is mutated as new children are created so subsequent
    sub-notes in the same atomize pass see them. ``disambiguate_on_collision``
    selects between SKIP-and-inline (default, False) and force-create-
    with-numeric-suffix (True) when a proposed child slug already
    exists somewhere in the vault.
    """
    try:
        text = md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("stage_c atomize: read failed for %s: %s", md, exc)
        return False
    fm, body = split_frontmatter(text)
    sections = _split_on_headings(body)
    if not sections:
        logger.info(
            "stage_c atomize-skipped-no-headings: %s has no ## sections",
            md.relative_to(vault),
        )
        return False

    parent_slug = md.stem
    parent_title = fm.get("title") or parent_slug
    children_rel: list[str] = []
    child_writes: list[tuple[pathlib.Path, str]] = []
    # slug → human-readable title, populated as children are built;
    # used to render the ``## Sub-notes`` bullets on the parent and to
    # sort them alphabetically per the parent-link-update protocol
    # (cortex-memory/research/2026-06-09-atomization-parent-link-update.md).
    child_titles: dict[str, str] = {}
    # Sections whose slug already lives somewhere in the vault and
    # where ``disambiguate_on_collision`` is False. We keep them inline
    # in the parent rather than atomize them out — losing their
    # content would be the worst possible failure mode.
    skipped_sections: list[tuple[str, str]] = []
    # Build child files first so we can journal them with their final paths.
    for title, section_text in sections:
        child_slug = f"{parent_slug}-{_slugify(title)}"
        child_path = md.parent / f"{child_slug}.md"
        # Don't clobber an existing file; suffix until unique.
        suffix = 2
        while child_path.exists() and child_path.resolve() != md.resolve():
            child_slug = f"{parent_slug}-{_slugify(title)}-{suffix}"
            child_path = md.parent / f"{child_slug}.md"
            suffix += 1

        # Vault-wide slug-uniqueness pre-flight. The same-directory
        # ``.exists()`` check above only catches clobbers; a note with
        # the same stem in a DIFFERENT folder produces a shadow orphan
        # (wikilinks resolve by slug regardless of folder, so the
        # newcomer becomes unreachable). See 2026-06-07 slug-dedup
        # recurrence analysis.
        collision_key = child_slug.lower()
        if collision_key in slug_index:
            existing_path = slug_index[collision_key]
            try:
                existing_rel = existing_path.relative_to(vault)
            except ValueError:
                existing_rel = existing_path
            if disambiguate_on_collision:
                disamb_suffix = 2
                while True:
                    candidate_slug = f"{child_slug}-{disamb_suffix}"
                    candidate_path = md.parent / f"{candidate_slug}.md"
                    if (
                        candidate_slug.lower() not in slug_index
                        and not candidate_path.exists()
                    ):
                        break
                    disamb_suffix += 1
                logger.info(
                    "stage_c atomize-slug-disambiguated: %s section %r -> %s "
                    "(collision with %s)",
                    md.relative_to(vault),
                    title,
                    candidate_slug,
                    existing_rel,
                )
                child_slug = candidate_slug
                child_path = candidate_path
            else:
                logger.warning(
                    "stage_c atomize-skipped-slug-collision: slug %r already "
                    "exists (would create duplicate of %s) — leaving section "
                    "%r inline in %s",
                    child_slug,
                    existing_rel,
                    title,
                    md.relative_to(vault),
                )
                skipped_sections.append((title, section_text))
                continue

        child_fm: dict[str, Any] = {
            "title": title,
            "tags": fm.get("tags", []),
            "created": fm.get("created", today_iso),
            "updated": today_iso,
            "derived_from": parent_slug,
        }
        if isinstance(child_fm["tags"], str):
            child_fm["tags"] = [child_fm["tags"]]
        # Child body = section content + a ``## Related`` block pointing
        # back at the parent. The backlink closes the parent↔child loop
        # so children aren't structural orphans (parent-link-update
        # protocol step 4).
        child_body = (
            section_text.strip()
            + "\n\n## Related\n\n"
            + f"- [[{parent_slug}]] — {parent_title}\n"
        )
        child_writes.append(
            (child_path, _frontmatter_render(child_fm) + "\n" + child_body)
        )
        children_rel.append(str(child_path.relative_to(vault)))
        child_titles[child_slug] = title
        # Reserve the slug so a later sibling section in this same
        # atomize pass can't collide with it.
        slug_index[child_slug.lower()] = child_path

    # If literally every section collided and we're in SKIP mode,
    # there's nothing to atomize — leave the source unchanged so the
    # next tick can either re-evaluate against a freshly-groomed vault
    # or surface the collision via a separate cleanup pass.
    if not children_rel:
        logger.warning(
            "stage_c atomize-aborted-all-collisions: every section of %s "
            "collides with an existing vault slug; source left unchanged",
            md.relative_to(vault),
        )
        return False

    # Parent rewrite — replace the body with wikilinks. Keep the original
    # frontmatter intact (atomize is structural, not editorial); bump ``updated``.
    new_fm = dict(fm)
    new_fm["updated"] = today_iso
    parent_body_lines: list[str] = []
    # Preserve any pre-first-heading prologue from the original body —
    # that content has nowhere else to live.
    prologue_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            break
        prologue_lines.append(line)
    prologue = "\n".join(prologue_lines).strip()
    if prologue:
        parent_body_lines.append(prologue)
        parent_body_lines.append("")
    # ``## Sub-notes`` replaces the old ``## Sections`` block. Bullets
    # render as ``- [[slug]] — title`` (sorted alphabetically by slug
    # per parent-link-update protocol step 5) so a reader of the parent
    # can navigate to each child with its title visible. The intro line
    # is the canonical Stage-C-grooming marker described in the design.
    parent_body_lines.append("## Sub-notes")
    parent_body_lines.append("")
    parent_body_lines.append(
        "The following notes were atomized from this parent during Stage C grooming:"
    )
    parent_body_lines.append("")
    sub_note_bullets = [
        f"- [[{pathlib.Path(child_rel).stem}]] — "
        f"{child_titles.get(pathlib.Path(child_rel).stem, pathlib.Path(child_rel).stem)}"
        for child_rel in children_rel
    ]
    for bullet in sorted(sub_note_bullets):
        parent_body_lines.append(bullet)
    # Inline-preserved sections (slug-collision SKIPs) — append after
    # the Sections list so the parent still reads top-to-bottom.
    for title, section_text in skipped_sections:
        parent_body_lines.append("")
        parent_body_lines.append(section_text.rstrip("\n"))
    new_parent_text = (
        _frontmatter_render(new_fm)
        + "\n"
        + "\n".join(parent_body_lines)
        + "\n"
    )

    # Journal BEFORE the write.
    original_sha = _sha256_of_text(text)
    entry = None
    if journal_path is not None:
        entry = journal_mod.append(
            journal_path,
            op="atomize",
            source=str(md.relative_to(vault)),
            targets=children_rel,
            detail={
                "parent": parent_slug,
                "children": [pathlib.Path(c).stem for c in children_rel],
                "original_content_sha": original_sha,
            },
        )

    try:
        # Children are new files in the same directory; lock each
        # individually so a concurrent atomize on a different parent
        # that would land at the same child name (rare collision)
        # serializes too.
        for child_path, child_text in child_writes:
            with vault_lock.acquire(
                child_path,
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ):
                child_path.write_text(child_text, encoding="utf-8")
        # Parent rewrite happens under the source lock the caller holds —
        # no extra acquire needed.
        md.write_text(new_parent_text, encoding="utf-8")
    except (OSError, vault_lock.VaultLockTimeout) as exc:
        logger.warning("stage_c atomize: write failed for %s: %s", md, exc)
        return False

    if journal_path is not None and entry is not None:
        journal_mod.commit(journal_path, entry.journal_id)
    return True


# ---------- archive ----------


def _rewrite_wikilinks_in_file(
    path: pathlib.Path,
    old_slug: str,
    new_slug: str,
) -> bool:
    """Rewrite ``[[old_slug]]`` (and aliased/folder-qualified forms) to
    ``[[new_slug]]`` inside ``path``. Returns True if the file changed.

    Conservative — only rewrites the slug portion, leaving any
    ``#anchor`` and ``|alias`` parts alone.

    Acquires an EXCLUSIVE :func:`vault_lock` on ``path`` for the
    read-modify-write window — these calls reach into arbitrary
    referrer files across the vault during archive/dedupe, so each
    one must serialize against a thinking-side write to the same
    file. Returns False on lock timeout (treated as "skip this
    referrer, the next cycle picks it up").
    """
    try:
        with vault_lock.acquire(
            path,
            mode=vault_lock.LockMode.EXCLUSIVE,
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return False

            def repl(match: re.Match[str]) -> str:
                target = match.group(1).strip()
                base = target.rsplit("/", 1)[-1]
                if base != old_slug:
                    return match.group(0)
                # Reconstruct with the new slug. Anchor / alias survive
                # in match.group(0) — we replace only the slug section.
                full = match.group(0)
                return full.replace(target, new_slug, 1)

            new_text = _WIKILINK_RE.sub(repl, text)
            if new_text != text:
                path.write_text(new_text, encoding="utf-8")
                return True
            return False
    except vault_lock.VaultLockTimeout as exc:
        logger.warning(
            "stage_c wikilink-rewrite: lock timeout on %s: %s", path, exc
        )
        return False


def archive(
    mind: pathlib.Path,
    max_items: int,
    journal_path: Optional[pathlib.Path],
    today: Optional[datetime.date] = None,
) -> int:
    """Move stale dailies into ``archive/dailies/<year>/`` and rewrite
    wikilinks that pointed at them.

    Returns the count of dailies archived this cycle.
    """
    vault = _vault_dir(mind)
    dailies = vault / "dailies"
    if not dailies.is_dir():
        return 0

    today = today or _today()
    cutoff = today - datetime.timedelta(days=STALE_DAILY_DAYS)
    cutoff_str = cutoff.isoformat()
    processed = 0

    archive_root = _archive_dailies_dir(mind)
    for md in sorted(dailies.glob("*.md")):
        if processed >= max_items:
            break
        stem = md.stem
        if len(stem) < 10 or stem[:10] >= cutoff_str:
            continue
        # Filename year prefix is YYYY. We rely on the canonical
        # naming convention; anything else is skipped.
        year = stem[:4]
        if not year.isdigit():
            continue
        dst_dir = archive_root / year
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / md.name
        if dst.exists():
            logger.info(
                "stage_c archive: destination already exists, skipping %s",
                md.relative_to(vault),
            )
            continue

        # Find inbound wikilinks across the vault that point at this slug.
        wikilink_updates: list[dict[str, str]] = []
        new_slug = f"archive/dailies/{year}/{md.stem}"
        for source in vault.rglob("*.md"):
            if source.resolve() == md.resolve():
                continue
            try:
                text = source.read_text(encoding="utf-8")
            except OSError:
                continue
            if f"[[{md.stem}" not in text:
                continue
            wikilink_updates.append(
                {
                    "file": str(source.relative_to(vault)),
                    "old": md.stem,
                    "new": new_slug,
                }
            )

        original_text = ""
        try:
            original_text = md.read_text(encoding="utf-8")
        except OSError:
            pass

        entry = None
        if journal_path is not None:
            entry = journal_mod.append(
                journal_path,
                op="archive",
                source=str(md.relative_to(vault)),
                targets=[str(dst.relative_to(vault))],
                detail={
                    "src": str(md.relative_to(vault)),
                    "dst": str(dst.relative_to(vault)),
                    "wikilink_updates": wikilink_updates,
                    "src_sha": _sha256_of_text(original_text),
                },
            )

        # Lock both endpoints of the rename. Path-order acquisition
        # (alphabetical) avoids deadlock against any other archive-style
        # op touching the same files. ``dst`` doesn't exist yet — that's
        # fine, :func:`vault_lock.acquire` attaches the lock to a
        # sidecar created on demand.
        rename_paths = sorted([md, dst])
        try:
            with vault_lock.acquire(
                rename_paths[0],
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ), vault_lock.acquire(
                rename_paths[1],
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ):
                md.rename(dst)
        except vault_lock.VaultLockTimeout as exc:
            logger.warning(
                "stage_c archive: lock timeout on rename %s -> %s: %s",
                md,
                dst,
                exc,
            )
            continue
        except OSError as exc:
            logger.warning("stage_c archive: rename failed %s -> %s: %s", md, dst, exc)
            continue

        for upd in wikilink_updates:
            target_path = vault / upd["file"]
            _rewrite_wikilinks_in_file(target_path, upd["old"], upd["new"])

        if journal_path is not None and entry is not None:
            journal_mod.commit(journal_path, entry.journal_id)
        processed += 1
    return processed


# ---------- archive-stale-open ----------


def _daily_path_for(mind: pathlib.Path, day: datetime.date) -> pathlib.Path:
    return mind / "cortex-memory" / "dailies" / f"{day.isoformat()}.md"


def _ensure_daily_for(mind: pathlib.Path, day: datetime.date) -> pathlib.Path:
    """Create today's daily from the canonical template if missing.

    Mirrors :func:`stage_b._ensure_daily` — keeping the helper local
    avoids a cross-module import for a single call site.
    """
    path = _daily_path_for(mind, day)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    iso = day.isoformat()
    template = (
        "---\n"
        f"title: {iso}\n"
        "tags: [daily]\n"
        f"created: {iso}\n"
        f"updated: {iso}\n"
        f"last_accessed: {iso}\n"
        "access_count: 0\n"
        "---\n"
        "\n"
        f"# {iso}\n"
        "\n"
    )
    path.write_text(template, encoding="utf-8")
    return path


def _append_daily_line(mind: pathlib.Path, day: datetime.date, line: str) -> None:
    """Append ``line`` (without trailing newline) to today's daily."""
    path = _ensure_daily_for(mind, day)
    existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + line + "\n", encoding="utf-8")


def archive_stale_open(
    mind: pathlib.Path,
    max_items: int,
    journal_path: Optional[pathlib.Path],
    *,
    today: Optional[datetime.date] = None,
    dry_run: bool = DEFAULT_ARCHIVE_STALE_OPEN_DRY_RUN,
) -> int:
    """Flip ``status: open`` to ``status: archived`` on stale, unread notes.

    Selection rule (UNION of all three):

    * ``status: open`` (case-insensitive frontmatter value)
    * ``access_count`` is 0, absent, or non-numeric
    * ``created`` is more than :data:`STALE_OPEN_AGE_DAYS` days ago

    With ``dry_run=True`` (the default), candidates are counted and
    logged via :mod:`logger`, but neither frontmatter nor the daily is
    mutated. With ``dry_run=False`` the rule rewrites frontmatter (sets
    ``status: archived``, bumps ``updated``) and appends one bullet to
    today's daily per archived note. Idempotent: a note whose status
    has already been set to ``archived`` falls out of the candidate
    set and is skipped on subsequent ticks.

    Returns the count of candidates handled this cycle (matched, in
    dry-run; mutated, in apply). Capped at ``max_items``.
    """
    vault = _vault_dir(mind)
    if not vault.is_dir():
        return 0

    today = today or _today()
    cutoff = today - datetime.timedelta(days=STALE_OPEN_AGE_DAYS)
    today_iso = today.isoformat()

    processed = 0
    for md in _iter_groomable_notes(vault):
        if processed >= max_items:
            break
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        status = str(fm.get("status") or "").strip().lower()
        if status != "open":
            continue
        try:
            ac = int(fm.get("access_count") or 0)
        except (TypeError, ValueError):
            ac = 0
        if ac != 0:
            continue
        created = _parse_created_date(fm.get("created"))
        if created is None or created > cutoff:
            continue

        rel = str(md.relative_to(vault))
        created_str = created.isoformat()
        if dry_run:
            logger.info(
                "stage_c archive-stale-open[dry-run]: candidate %s "
                "(status=open, access_count=0, created=%s)",
                rel,
                created_str,
            )
            processed += 1
            continue

        new_fm = dict(fm)
        new_fm["status"] = "archived"
        new_fm["updated"] = today_iso
        new_text = _frontmatter_render(new_fm) + "\n" + body.lstrip("\n")
        original_sha = _sha256_of_text(text)

        entry = None
        if journal_path is not None:
            entry = journal_mod.append(
                journal_path,
                op="archive-stale-open",
                source=rel,
                targets=[rel],
                detail={
                    "src": rel,
                    "src_sha": original_sha,
                    "created": created_str,
                    "rule": "stale-open-cleanup",
                },
            )

        try:
            with vault_lock.acquire(
                md,
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ):
                md.write_text(new_text, encoding="utf-8")
        except (OSError, vault_lock.VaultLockTimeout) as exc:
            logger.warning(
                "stage_c archive-stale-open: write failed for %s: %s",
                rel,
                exc,
            )
            continue

        daily_line = (
            f"- Archived {md.stem} "
            f"(status: open, access_count: 0, created: {created_str}) "
            f"— stale-open-cleanup"
        )
        try:
            _append_daily_line(mind, today, daily_line)
        except OSError as exc:
            logger.warning(
                "stage_c archive-stale-open: daily-append failed for %s: %s",
                rel,
                exc,
            )

        if journal_path is not None and entry is not None:
            journal_mod.commit(journal_path, entry.journal_id)
        processed += 1

    return processed


# ---------- dedupe-merge ----------


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein. Small strings only — vault titles are
    short, this is fine for O(n*m) where n,m < ~80 chars."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,        # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution
            )
        prev = cur
    return prev[-1]


def _parse_created_date(raw: Any) -> Optional[datetime.date]:
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) < 10:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _dedupe_groups(
    notes: list[pathlib.Path],
) -> list[list[pathlib.Path]]:
    """Group notes by (a) exact slug-stem match and (b) fuzzy title.

    Returns a list of groups, each of length >= 2. Singletons are
    dropped. A note belongs to at most one group; slug-stem groups
    are formed first, then remaining singletons are pairwise compared
    for fuzzy-title proximity.
    """
    by_stem: dict[str, list[pathlib.Path]] = defaultdict(list)
    for md in notes:
        by_stem[md.stem.lower()].append(md)

    groups: list[list[pathlib.Path]] = []
    leftover: list[pathlib.Path] = []
    for stem_key, group in by_stem.items():
        if len(group) >= 2:
            groups.append(group)
        else:
            leftover.extend(group)

    # Title-fuzzy pass: O(n^2) — fine for n in the hundreds.
    titles: list[tuple[pathlib.Path, str]] = []
    for md in leftover:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        title = str(fm.get("title") or md.stem).strip().lower()
        if title:
            titles.append((md, title))

    used: set[pathlib.Path] = set()
    for i, (md_i, title_i) in enumerate(titles):
        if md_i in used:
            continue
        cluster: list[pathlib.Path] = [md_i]
        for md_j, title_j in titles[i + 1 :]:
            if md_j in used:
                continue
            if title_i == title_j or _levenshtein(title_i, title_j) < FUZZY_TITLE_DISTANCE:
                cluster.append(md_j)
        if len(cluster) >= 2:
            for m in cluster:
                used.add(m)
            groups.append(cluster)
    return groups


def _pick_canonical(
    group: list[pathlib.Path],
    inbound: dict[str, set[str]],
) -> pathlib.Path:
    """Canonical = oldest ``created:``; tie-break on inbound link count;
    final tie-break on alphabetical slug."""

    def sort_key(md: pathlib.Path) -> tuple[str, int, str]:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            text = ""
        fm, _body = split_frontmatter(text)
        created = _parse_created_date(fm.get("created"))
        created_str = created.isoformat() if created else "9999-99-99"
        # Negative inbound count so MORE inbound sorts earlier (canonical).
        inbound_count = len(inbound.get(md.stem, set()) | inbound.get(md.stem.lower(), set()))
        return (created_str, -inbound_count, md.stem)

    return sorted(group, key=sort_key)[0]


def dedupe_merge(
    mind: pathlib.Path,
    max_items: int,
    journal_path: Optional[pathlib.Path],
) -> int:
    """Merge duplicate notes (slug-equal OR fuzzy title-equal).

    Returns the count of duplicate notes consumed (not the number of
    groups). Each group of N produces N-1 deletes.
    """
    vault = _vault_dir(mind)
    today_iso = _today().isoformat()
    notes = _iter_groomable_notes(vault)
    if not notes:
        return 0

    inbound = _build_inbound_index(vault)
    groups = _dedupe_groups(notes)
    if not groups:
        return 0

    processed = 0
    for group in groups:
        if processed >= max_items:
            break
        canonical = _pick_canonical(group, inbound)
        duplicates = [m for m in group if m != canonical]
        if not duplicates:
            continue

        # Acquire EXCLUSIVE locks on canonical + every duplicate up
        # front, in path order. This prevents another writer (thinking,
        # a concurrent worker tick) from mutating any of these files
        # between our reads and the merged-write/unlink. Path-order
        # acquisition keeps multiple dedupe-merge calls deadlock-free
        # when they pick overlapping groups.
        all_paths = sorted({canonical, *duplicates})
        try:
            with contextlib.ExitStack() as stack:
                for p in all_paths:
                    stack.enter_context(
                        vault_lock.acquire(
                            p,
                            mode=vault_lock.LockMode.EXCLUSIVE,
                            timeout=_LOCK_TIMEOUT_SECONDS,
                        )
                    )

                try:
                    canon_text = canonical.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "stage_c dedupe: canonical read failed %s: %s",
                        canonical,
                        exc,
                    )
                    continue
                canon_fm, canon_body = split_frontmatter(canon_text)

                merged_slugs: list[str] = []
                merged_sections: list[str] = []
                wikilink_updates: list[dict[str, str]] = []

                for dup in duplicates:
                    if processed >= max_items:
                        break
                    try:
                        dup_text = dup.read_text(encoding="utf-8")
                    except OSError as exc:
                        logger.warning(
                            "stage_c dedupe: dup read failed %s: %s", dup, exc
                        )
                        continue
                    dup_fm, dup_body = split_frontmatter(dup_text)
                    dup_created = str(dup_fm.get("created", "unknown"))
                    section = (
                        f"\n## Merged from {dup.stem} ({dup_created})\n\n"
                        f"{dup_body.strip()}\n"
                    )
                    merged_sections.append(section)
                    merged_slugs.append(dup.stem)

                    # Wikilinks across vault pointing at duplicate → canonical.
                    for source in vault.rglob("*.md"):
                        if source.resolve() in (
                            canonical.resolve(),
                            dup.resolve(),
                        ):
                            continue
                        try:
                            text = source.read_text(encoding="utf-8")
                        except OSError:
                            continue
                        if f"[[{dup.stem}" not in text:
                            continue
                        wikilink_updates.append(
                            {
                                "file": str(source.relative_to(vault)),
                                "old": dup.stem,
                                "new": canonical.stem,
                            }
                        )

                if not merged_slugs:
                    continue

                # Bump canonical frontmatter, append merged sections.
                new_canon_fm = dict(canon_fm)
                new_canon_fm["updated"] = today_iso
                new_canon_fm["last_accessed"] = today_iso
                try:
                    ac = int(new_canon_fm.get("access_count") or 0)
                except (TypeError, ValueError):
                    ac = 0
                new_canon_fm["access_count"] = ac + 1
                new_canon_body = (
                    canon_body.rstrip() + "\n" + "".join(merged_sections)
                )
                new_canon_text = (
                    _frontmatter_render(new_canon_fm) + "\n" + new_canon_body
                )
                if not new_canon_text.endswith("\n"):
                    new_canon_text += "\n"

                entry = None
                if journal_path is not None:
                    entry = journal_mod.append(
                        journal_path,
                        op="dedupe-merge",
                        source=str(canonical.relative_to(vault)),
                        targets=[str(canonical.relative_to(vault))],
                        detail={
                            "canonical": canonical.stem,
                            "merged": merged_slugs,
                            "wikilink_updates": wikilink_updates,
                        },
                    )

                try:
                    canonical.write_text(new_canon_text, encoding="utf-8")
                    for dup in duplicates:
                        if dup.stem in merged_slugs and dup.exists():
                            dup.unlink()
                except OSError as exc:
                    logger.warning(
                        "stage_c dedupe: write/unlink failed: %s", exc
                    )
                    continue
        except vault_lock.VaultLockTimeout as exc:
            logger.warning(
                "stage_c dedupe: lock timeout on canonical=%s duplicates=%s: %s",
                canonical,
                [d.stem for d in duplicates],
                exc,
            )
            continue

        # Referrer rewrites happen outside the canonical-group locks —
        # ``_rewrite_wikilinks_in_file`` acquires its own per-file lock.
        for upd in wikilink_updates:
            target_path = vault / upd["file"]
            _rewrite_wikilinks_in_file(target_path, upd["old"], upd["new"])

        if journal_path is not None and entry is not None:
            journal_mod.commit(journal_path, entry.journal_id)

        processed += len(merged_slugs)
    return processed


# ---------- orphan-resolve ----------


def _tags_of(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("tags")
    if isinstance(raw, list):
        return [str(t).strip().lower() for t in raw if str(t).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip().lower()]
    return []


_PARENT_FOLDERS = ("projects", "reference", "people")


def _parent_candidates(vault: pathlib.Path, tag: str) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for folder in _PARENT_FOLDERS:
        candidate = vault / folder / f"{tag}.md"
        if candidate.is_file():
            out.append(candidate)
    return out


def _append_linked_notes_section(parent: pathlib.Path, link_line: str) -> bool:
    """Append ``link_line`` (no trailing newline) under a
    ``## Linked notes`` section in ``parent``. Creates the section if
    missing. Idempotent: returns False if the exact line already exists
    anywhere in the file.

    Holds an EXCLUSIVE :func:`vault_lock` on ``parent`` for the
    read-modify-write window so a thinking-side append to the same
    parent can't land in the middle of our rewrite. Returns False on
    lock timeout (orphan-resolve treats this the same as "already
    linked" — retry next cycle).
    """
    try:
        with vault_lock.acquire(
            parent,
            mode=vault_lock.LockMode.EXCLUSIVE,
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            try:
                text = parent.read_text(encoding="utf-8")
            except OSError:
                return False
            if link_line in text:
                return False
            if "## Linked notes" in text:
                # Append directly after the section header (or at end).
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if line.strip() == "## Linked notes":
                        # Find end of the section: next "## " heading or EOF.
                        j = i + 1
                        while j < len(lines) and not (
                            lines[j].startswith("## ")
                            and not lines[j].startswith("### ")
                        ):
                            j += 1
                        lines.insert(j, link_line)
                        new_text = "\n".join(lines)
                        if not new_text.endswith("\n"):
                            new_text += "\n"
                        parent.write_text(new_text, encoding="utf-8")
                        return True
            # No section — append at EOF.
            if text and not text.endswith("\n"):
                text += "\n"
            parent.write_text(
                text + "\n## Linked notes\n\n" + link_line + "\n",
                encoding="utf-8",
            )
            return True
    except vault_lock.VaultLockTimeout as exc:
        logger.warning(
            "stage_c orphan-link: lock timeout on parent %s: %s", parent, exc
        )
        return False


def orphan_resolve(
    mind: pathlib.Path,
    max_items: int,
    journal_path: Optional[pathlib.Path],
) -> int:
    """Auto-link tag-matched orphans to a single parent; queue
    ambiguous ones to ``inner/orphans-pending.md``.

    Returns the count of orphans resolved (linked + queued) this cycle.
    """
    vault = _vault_dir(mind)
    inbound = _build_inbound_index(vault)
    processed = 0
    pending_lines: list[str] = []

    for md in _iter_groomable_notes(vault):
        if processed >= max_items:
            break
        if md.stem in inbound or md.stem.lower() in inbound:
            continue
        # Skip notes that *are* parent candidates themselves — they
        # legitimately have no inbound link until first reference.
        rel_parts = md.relative_to(vault).parts
        if rel_parts and rel_parts[0] in _PARENT_FOLDERS:
            continue

        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        tags = _tags_of(fm)

        # Find parent candidates: tag → projects/<tag>.md OR reference/<tag>.md OR people/<tag>.md.
        all_candidates: list[pathlib.Path] = []
        for tag in tags:
            all_candidates.extend(_parent_candidates(vault, tag))
        # Dedupe while preserving order.
        seen: set[pathlib.Path] = set()
        unique = [c for c in all_candidates if not (c in seen or seen.add(c))]

        if len(unique) == 1:
            parent = unique[0]
            link_line = f"- [[{md.stem}]]"
            changed = _append_linked_notes_section(parent, link_line)
            if not changed:
                # Already linked — still counts as a "resolved" pass.
                processed += 1
                continue
            if journal_path is not None:
                entry = journal_mod.append(
                    journal_path,
                    op="orphan-link",
                    source=str(md.relative_to(vault)),
                    targets=[str(parent.relative_to(vault))],
                    detail={
                        "orphan": md.stem,
                        "parent": parent.stem,
                        "line": link_line,
                    },
                )
                journal_mod.commit(journal_path, entry.journal_id)
            processed += 1
        else:
            # Zero or multiple parents — queue.
            tag_repr = ", ".join(tags) if tags else "(none)"
            pending_lines.append(f"- [[{md.stem}]] (tags: {tag_repr})")
            processed += 1

    if pending_lines:
        path = _orphans_pending_path(mind)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock the pending-queue file: orphan-resolve can append from
        # multiple cadence ticks if a backlog spills over, and we don't
        # want two appenders interleaving lines.
        try:
            with vault_lock.acquire(
                path,
                mode=vault_lock.LockMode.EXCLUSIVE,
                timeout=_LOCK_TIMEOUT_SECONDS,
            ):
                existing = ""
                if path.is_file():
                    try:
                        existing = path.read_text(encoding="utf-8")
                    except OSError:
                        existing = ""
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                path.write_text(
                    existing + "\n".join(pending_lines) + "\n",
                    encoding="utf-8",
                )
        except vault_lock.VaultLockTimeout as exc:
            logger.warning(
                "stage_c orphan-resolve: lock timeout on pending queue %s: %s",
                path,
                exc,
            )
    return processed


# ---------- verifiers ----------
#
# Verifiers are bound to a concrete ``mind`` root via the closure
# pattern in :func:`register_verifiers`. The journal module's verifier
# signature is ``(entry) -> bool`` — it has no way to receive the
# vault root, so the closure captures it.


def register_verifiers(mind: pathlib.Path) -> None:
    """Bind Stage C verifiers to a concrete ``mind`` root.

    Called once at worker startup BEFORE :func:`journal.replay`. The
    closure form captures the vault root so verifiers can resolve
    relative paths from journal entries.
    """
    vault = _vault_dir(mind)

    def atomize_verifier(entry: journal_mod.JournalEntry) -> bool:
        # All child targets must exist.
        for target_rel in entry.targets:
            if not (vault / target_rel).is_file():
                return False
        # Parent must reference each child via wikilink.
        parent_path = vault / entry.source
        if not parent_path.is_file():
            return False
        try:
            text = parent_path.read_text(encoding="utf-8")
        except OSError:
            return False
        for child_slug in entry.detail.get("children", []):
            if f"[[{child_slug}]]" not in text:
                return False
        return True

    def archive_verifier(entry: journal_mod.JournalEntry) -> bool:
        src = vault / entry.detail.get("src", entry.source)
        dst = vault / entry.detail.get("dst", entry.targets[0] if entry.targets else "")
        if src.exists():
            return False
        if not dst.is_file():
            return False
        return True

    def dedupe_verifier(entry: journal_mod.JournalEntry) -> bool:
        canonical_slug = entry.detail.get("canonical")
        merged_slugs = entry.detail.get("merged", [])
        if not canonical_slug:
            return False
        # Canonical file must exist with merge sections; each merged
        # file must NOT exist (was deleted).
        canon_path = vault / entry.source
        if not canon_path.is_file():
            return False
        try:
            text = canon_path.read_text(encoding="utf-8")
        except OSError:
            return False
        for slug in merged_slugs:
            if f"## Merged from {slug}" not in text:
                return False
        return True

    def orphan_link_verifier(entry: journal_mod.JournalEntry) -> bool:
        parent_rel = entry.targets[0] if entry.targets else ""
        link_line = entry.detail.get("line", "")
        if not parent_rel or not link_line:
            return False
        parent_path = vault / parent_rel
        if not parent_path.is_file():
            return False
        try:
            text = parent_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return link_line in text

    def archive_stale_open_verifier(entry: journal_mod.JournalEntry) -> bool:
        src_rel = entry.detail.get("src", entry.source)
        src_path = vault / src_rel
        if not src_path.is_file():
            return False
        try:
            text = src_path.read_text(encoding="utf-8")
        except OSError:
            return False
        fm, _body = split_frontmatter(text)
        return str(fm.get("status") or "").strip().lower() == "archived"

    journal_mod.register_verifier("atomize", atomize_verifier)
    journal_mod.register_verifier("archive", archive_verifier)
    journal_mod.register_verifier("archive-stale-open", archive_stale_open_verifier)
    journal_mod.register_verifier("dedupe-merge", dedupe_verifier)
    journal_mod.register_verifier("orphan-link", orphan_link_verifier)


# ---------- correction cascade check ----------


def correction_cascade_check(
    mind: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
) -> "cascade_mod.CascadeReport":
    """Public wrapper: run correction cascade detection as a standalone
    Stage C operation.

    This is the hook that the memory worker's grooming pipeline calls
    during the link-audit phase (after atomize, before dedupe).
    Standalone invocation is for ad-hoc runs or testing.
    """
    return cascade_mod.run(
        mind,
        journal_path=journal_path,
        surface_path=mind / "inner" / "surface" / "correction-cascade-grooming-report.md",
        daily_path=mind / "cortex-memory" / "dailies" / f"{_today().isoformat()}.md",
    )


# ---------- top-level run ----------


def _emit_decay_recovery_event(
    mind: pathlib.Path,
    notes_recovered: int,
    cycle_duration_seconds: float,
) -> None:
    """Append a ``decay_recovery_rate`` record to events.jsonl.

    Best-effort: write failure is logged and swallowed (the loop
    must not crash on observability).
    """
    rate = (notes_recovered / cycle_duration_seconds) if cycle_duration_seconds > 0 else 0.0
    record = {
        "ts": _utc_iso(),
        "type": "decay_recovery_rate",
        "notes_recovered": int(notes_recovered),
        "cycle_duration_seconds": round(float(cycle_duration_seconds), 3),
        "rate": round(float(rate), 6),
    }
    path = _events_jsonl_path(mind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("stage_c: failed to append decay_recovery_rate event: %s", exc)


def _load_stage_c_config(mind: pathlib.Path) -> StageCConfig:
    """Read ``memory_worker.stage_c.*`` overrides from
    ``alice.config.json``.

    Missing / malformed config returns module defaults — same shape as
    :func:`memory_worker.wake._load_config`.
    """
    cfg_path = mind / "config" / "alice.config.json"
    cfg = StageCConfig()
    if not cfg_path.is_file():
        return cfg
    try:
        blob = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return cfg
    section = ((blob or {}).get("memory_worker") or {}).get("stage_c") or {}
    if not isinstance(section, dict):
        return cfg
    if "decay_threshold" in section:
        try:
            cfg.decay_threshold = int(section["decay_threshold"])
        except (TypeError, ValueError):
            pass
    if "max_items_per_category" in section:
        try:
            cfg.max_items_per_category = int(section["max_items_per_category"])
        except (TypeError, ValueError):
            pass
    if "archive_stale_open_dry_run" in section:
        val = section["archive_stale_open_dry_run"]
        if isinstance(val, bool):
            cfg.archive_stale_open_dry_run = val
        elif isinstance(val, str):
            cfg.archive_stale_open_dry_run = val.strip().lower() not in (
                "false",
                "0",
                "no",
                "off",
            )
    if "atomize_disambiguate_on_collision" in section:
        val = section["atomize_disambiguate_on_collision"]
        if isinstance(val, bool):
            cfg.atomize_disambiguate_on_collision = val
        elif isinstance(val, str):
            cfg.atomize_disambiguate_on_collision = val.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            )
    return cfg


def run(
    mind: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
    config: Optional[StageCConfig] = None,
) -> StageCReport:
    """One Stage C tick.

    Computes the trigger state, short-circuits if no signal fires,
    otherwise runs the four grooming operations in order with the
    per-category cap from ``config``. Emits a
    ``decay_recovery_rate`` event regardless of whether work happened
    (zero-rate samples are useful for tracking drought duration).
    """
    cfg = config or _load_stage_c_config(mind)
    report = StageCReport()
    state = compute_state(mind)
    if not should_run_c(state, cfg):
        logger.info(
            "stage_c: trigger inactive — bloated=%d stale=%d decayed=%d orphans=%d broken=%d (threshold=%d)",
            state.bloated_notes,
            state.stale_dailies,
            state.decayed_notes_in_window,
            state.orphans,
            state.broken_wikilinks,
            cfg.decay_threshold,
        )
        return report

    report.ran = True
    started = time.monotonic()
    report.atomize = atomize(
        mind,
        cfg.max_items_per_category,
        journal_path,
        disambiguate_on_collision=cfg.atomize_disambiguate_on_collision,
    )
    report.archive = archive(mind, cfg.max_items_per_category, journal_path)
    # Link audit phase: correction cascade detection (after atomize,
    # before dedupe). Runs even if no grooming mutations occurred —
    # detection is read-only and provides observability into the
    # vault's correction graph regardless of whether Stage C mutated
    # any files this cycle.
    cascade_report = cascade_mod.run(
        mind,
        journal_path=journal_path,
        surface_path=mind / "inner" / "surface" / "correction-cascade-grooming-report.md",
        daily_path=mind / "cortex-memory" / "dailies" / f"{_today().isoformat()}.md",
    )
    report.correction_pairs_checked = cascade_report.correction_pairs_checked
    report.unpropagated_corrections = cascade_report.total_unpropagated
    report.archive_stale_open = archive_stale_open(
        mind,
        cfg.max_items_per_category,
        journal_path,
        dry_run=cfg.archive_stale_open_dry_run,
    )
    report.dedupe_merge = dedupe_merge(mind, cfg.max_items_per_category, journal_path)
    report.orphan_resolve = orphan_resolve(mind, cfg.max_items_per_category, journal_path)
    elapsed = time.monotonic() - started

    total_recovered = (
        report.atomize
        + report.archive
        + report.archive_stale_open
        + report.dedupe_merge
        + report.orphan_resolve
    )
    _emit_decay_recovery_event(mind, total_recovered, elapsed)
    return report


__all__ = [
    "BLOATED_LINE_THRESHOLD",
    "DECAY_WINDOW_DAYS",
    "DEFAULT_ARCHIVE_STALE_OPEN_DRY_RUN",
    "DEFAULT_ATOMIZE_DISAMBIGUATE_ON_COLLISION",
    "DEFAULT_DECAY_THRESHOLD",
    "DEFAULT_MAX_ITEMS_PER_CATEGORY",
    "FITNESS_TAGS",
    "FUZZY_TITLE_DISTANCE",
    "STALE_DAILY_DAYS",
    "STALE_OPEN_AGE_DAYS",
    "StageCConfig",
    "StageCReport",
    "StageCState",
    "archive",
    "archive_stale_open",
    "atomize",
    "compute_state",
    "correction_cascade_check",
    "dedupe_merge",
    "orphan_resolve",
    "register_verifiers",
    "run",
    "should_run_c",
    "vault_has_slug",
]
