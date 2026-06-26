"""Birth signal — zero-access note detection and classification.

Observability-only complement to ``count_access_decay`` in
``metrics.vault_health``. Where ``count_access_decay`` catches the
"touched → cold" path (notes that were once accessed and have since
stopped), the birth signal catches the "birth → zero" path: notes that
have never been accessed since they were created.

Per the spec
([[cortex-memory/research/2026-06-25-birth-signal-implementation-spec]]),
this is a **detection + classification + report** pass. There is
intentionally no archival action and no bridge-builder side effect —
those live downstream and are wired in a later PR.

The module produces one ``birth_signal`` event per run and appends it
to ``events.jsonl`` (mirroring how ``vault_health`` writes its own
event). The event carries the bucket counts the spec calls for so
correlation against the vault_health stream is straightforward.

Buckets:

- **A — burst_artifact**: created in a ≥ 5-notes-in-24h burst window,
  has investigation-topic trigger_keywords, and has no inbound link
  from a note created after it. This is the strong-signal "throwaway
  research scratchpad" pattern.
- **B — useful_poorly_linked**: everything else (operational keywords,
  or non-burst origin, or inbound links from later notes). The
  ``reference/`` folder is always Bucket B — those are evergreen notes
  that lost discoverability, not artifacts.
- **ambiguous**: zero-access >30d but doesn't cleanly fit A or B. The
  pure spec definition (the falls-through case in
  :func:`classify_note`) is "burst-day origin, no inbound, but no
  trigger_keywords at all (or unclassified keywords)".

Same vault-walking conventions as ``vault_health`` (``_iter_notes``,
``_parse_created_date``, ``EXCLUDED_NAMES``) so the two metrics see the
same vault.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from indexer.yaml_lite import extract_wikilinks, split_frontmatter

from metrics.vault_health import (
    EXCLUDED_NAMES,
    _local_now,
    _parse_access_count,
    _parse_created_date,
    _read_text,
    _slug_from_fm,
    _strip_html_comments,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants

#: Notes younger than this aren't candidates — they're still in their
#: natural discovery window. The spec validated 30 days against vault
#: data: 51.7% of zero-access notes are >= 30 days old, and the
#: 14-day variant catches zero notes by the time the daily scan runs.
DEFAULT_ZERO_ACCESS_AGE_DAYS = 30

#: ≥ 5 notes created on the same calendar day = a burst session. The
#: April 26-29 thinking sessions produced 144/159/115/100 notes per day,
#: well above this floor. Empirically validated against 50 distinct
#: burst days in the vault.
BURST_SESSION_MIN_NOTES = 5

#: Excluded top-level folders. Mirrors the convention in
#: ``vault_health.count_access_decay`` and ``compute_continuous_checks``.
#: ``daily/`` (singular) is the legacy folder; ``dailies/`` is the
#: current name. ``index`` and ``README`` are vault scaffolding (already
#: in ``EXCLUDED_NAMES`` but listed here for the discovery-window
#: rule below).
_EXCLUDED_FOLDERS = frozenset(
    {"dailies", "daily", "archive", "gh-state", "experiments"}
)

#: Special-name files that aren't real notes — vault scaffolding /
#: navigation pages. We exclude ``index.md`` / ``README.md`` /
#: ``unresolved.md`` via ``EXCLUDED_NAMES``; this set additionally drops
#: any markdown file whose stem starts with ``index`` (e.g.
#: ``decisions-index.md``) — those are TOC notes, not artifacts.
_INDEX_STEM_PREFIX = ("index", "_index")

#: The reference/ folder is ALWAYS bucket B per the design note:
#: reference/ notes that have gone zero-access are evergreen notes that
#: lost discoverability, not burst artifacts. They need structural
#: repair (bridge), never archival.
_BUCKET_B_FORCED_FOLDERS = frozenset({"reference"})


# ---------------------------------------------------------------------------
# Default keyword pattern library
#
# Spec calls for an external YAML file
# (``cortex-memory/reference/birth-signal-patterns.yaml``) so the library
# can be reviewed quarterly without a code change. The module accepts an
# explicit ``--patterns`` path. When no YAML is on disk (e.g. first run,
# or fresh checkout in CI), these defaults — lifted verbatim from the
# spec — keep the module functional.

_DEFAULT_INVESTIGATION_KEYWORDS = frozenset(
    {
        "decay",
        "investigation",
        "pilot",
        "dry-run",
        "dry_run",
        "audit",
        "eval",
        "clustering",
        "cluster",
        "hebbian",
        "pagerank",
        "retrieval",
        "bm25",
        "fts5",
        "semantic",
        "structural",
        "bridge",
        "correction",
        "burst",
        "shadow",
        "orphan",
        "trigger",
        "reranker",
        "cue",
        "memory-worker",
        "metric",
        "analysis",
        "validation",
    }
)

_DEFAULT_OPERATIONAL_KEYWORDS = frozenset(
    {
        "protein",
        "workout",
        "cozyhem",
        "light",
        "home-assistant",
        "meal",
        "weight",
        "fitness",
        "jason",
        "katie",
        "signal",
        "hue",
        "theater",
        "coffee",
        "sleep",
        "food",
        "gainz",
        "protocol",
        "cut",
        "recovery",
        "lift",
        "bench",
        "squat",
        "deadlift",
        "row",
        "bike",
    }
)


def load_pattern_library(
    patterns_path: Path | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Load the (investigation, operational) keyword sets.

    When ``patterns_path`` is None or missing, fall back to the
    in-module defaults from the spec. The YAML loader is the cheap
    line-by-line parser so we don't drag in PyYAML for two flat lists.
    Matches the rest of the metrics package, which is intentionally
    stdlib-only.
    """
    if patterns_path is None or not patterns_path.exists():
        return _DEFAULT_INVESTIGATION_KEYWORDS, _DEFAULT_OPERATIONAL_KEYWORDS

    try:
        text = patterns_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "birth_signal: failed to read pattern library %s (%s); "
            "falling back to in-module defaults",
            patterns_path,
            exc,
        )
        return _DEFAULT_INVESTIGATION_KEYWORDS, _DEFAULT_OPERATIONAL_KEYWORDS

    investigation: set[str] = set()
    operational: set[str] = set()
    current: set[str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("investigation:"):
            current = investigation
            continue
        if line.startswith("operational:"):
            current = operational
            continue
        if line.lstrip().startswith("- ") and current is not None:
            kw = line.lstrip()[2:].strip().strip("\"'").lower()
            if kw:
                current.add(kw)
            continue
        # Anything else (top-level non-list scalar, comment-only line)
        # ends the current block.
        if not line.startswith(" ") and not line.startswith("\t"):
            current = None

    if not investigation:
        investigation = set(_DEFAULT_INVESTIGATION_KEYWORDS)
    if not operational:
        operational = set(_DEFAULT_OPERATIONAL_KEYWORDS)

    return frozenset(investigation), frozenset(operational)


# ---------------------------------------------------------------------------
# Vault walk + frontmatter helpers


def _iter_candidate_notes(vault_dir: Path) -> list[Path]:
    """Every ``*.md`` under ``vault_dir`` that's eligible for the birth
    signal scan.

    Excludes:
    - dotfile-prefixed paths (``.git/``, ``.obsidian/``, etc.)
    - ``EXCLUDED_NAMES`` (``index.md``, ``README.md``, ``unresolved.md``)
    - top-level ``_EXCLUDED_FOLDERS`` (dailies/archive/gh-state/experiments)
    - ``*-index.md`` and ``_index.md`` TOC scaffolding files

    The exclusion contract matches ``count_access_decay`` so the two
    metrics see the same vault.
    """
    if not vault_dir.exists():
        return []
    out: list[Path] = []
    for md in vault_dir.rglob("*.md"):
        rel_parts = md.relative_to(vault_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if md.name in EXCLUDED_NAMES:
            continue
        # Top-level folder exclusion.
        if rel_parts and rel_parts[0] in _EXCLUDED_FOLDERS:
            continue
        # Index / TOC scaffolding (e.g. decisions-index.md, _index.md).
        stem = md.stem.lower()
        if stem.endswith("-index") or any(
            stem.startswith(prefix) for prefix in _INDEX_STEM_PREFIX
        ):
            continue
        out.append(md)
    out.sort()
    return out


def _folder_of(rel_parts: tuple[str, ...]) -> str:
    """Top-level folder name for a vault-relative path, or empty."""
    if not rel_parts:
        return ""
    return rel_parts[0]


def _note_type_is_daily(fm: dict[str, Any]) -> bool:
    """``note_type: daily`` (string) or ``tags: [daily, ...]`` (list)."""
    nt = fm.get("note_type")
    if isinstance(nt, str) and nt.strip().lower() == "daily":
        return True
    tags = fm.get("tags")
    if isinstance(tags, list):
        return any(
            isinstance(t, str) and t.strip().lower() == "daily" for t in tags
        )
    return False


def _trigger_keywords_of(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("trigger_keywords")
    if isinstance(raw, list):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip().lower()]
    return []


# ---------------------------------------------------------------------------
# Detection


def detect_zero_access_notes(
    vault_dir: Path,
    *,
    age_days: int = DEFAULT_ZERO_ACCESS_AGE_DAYS,
    today: datetime | None = None,
) -> list[dict[str, Any]]:
    """Scan the vault and return zero-access notes older than ``age_days``.

    Each entry::

        {
            "path": Path,             # absolute path
            "rel": str,               # vault-relative path
            "slug": str,              # frontmatter slug or filename stem
            "folder": str,            # top-level folder
            "created": datetime,      # naive midnight on the created day
            "trigger_keywords": list[str],
            "fm": dict,               # raw frontmatter for downstream classification
        }

    Filters:
    - ``access_count == 0`` (missing / unparseable counts → skipped, NOT
      defaulted to 0; an unparseable count is data quality, not a signal)
    - ``created`` is parseable AND >= ``age_days`` ago
    - ``note_type`` is not ``daily``
    - ``status`` is not ``archived`` (avoid double-processing archive-pending notes)

    The minimum-discovery-window check from the spec (skip notes
    created in the last 24h) is implicit in ``age_days >= 1`` — at
    ``age_days = 30`` it's overwhelmingly satisfied.
    """
    if today is None:
        today = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    cutoff = today - timedelta(days=age_days)

    out: list[dict[str, Any]] = []
    for md in _iter_candidate_notes(vault_dir):
        text = _read_text(md)
        if not text:
            continue
        fm, _body = split_frontmatter(text)
        if not fm:
            continue
        if _note_type_is_daily(fm):
            continue
        status = fm.get("status")
        if isinstance(status, str) and status.strip().lower() == "archived":
            continue

        access_count = _parse_access_count(fm.get("access_count"))
        if access_count is None:
            # No parseable access_count → can't classify as zero-access.
            continue
        if access_count != 0:
            continue

        created = _parse_created_date(fm.get("created"))
        if created is None:
            continue
        if created > cutoff:
            continue

        rel = md.relative_to(vault_dir)
        out.append(
            {
                "path": md,
                "rel": str(rel),
                "slug": _slug_from_fm(fm, md.stem),
                "folder": _folder_of(rel.parts),
                "created": created,
                "trigger_keywords": _trigger_keywords_of(fm),
                "fm": fm,
            }
        )
    return out


def compute_burst_days(
    candidates: list[dict[str, Any]],
    *,
    min_notes: int = BURST_SESSION_MIN_NOTES,
) -> set[str]:
    """Return the set of ``YYYY-MM-DD`` strings flagged as burst days.

    A burst day is any calendar date that produced >= ``min_notes`` of
    the zero-access candidates. The empirical floor of 5 catches 50
    distinct days in the live vault; the April 26-29 thinking blitzes
    produced 144/159/115/100 notes/day, well above floor.
    """
    by_day: dict[str, int] = defaultdict(int)
    for note in candidates:
        by_day[note["created"].strftime("%Y-%m-%d")] += 1
    return {day for day, count in by_day.items() if count >= min_notes}


# ---------------------------------------------------------------------------
# Inbound-from-later detection
#
# Per the spec, the structural signal for Bucket A is "no inbound
# links from notes created AFTER this one was created". The
# vault_health module already has a generic inbound counter
# (count_inbound_links) but it doesn't carry the source's created
# date, so we walk the vault here with that dimension preserved.


def compute_inbound_from_later(
    vault_dir: Path,
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    """For each candidate slug, count inbound wikilinks from notes
    created on or after the candidate's ``created`` date.

    Returns ``{candidate_slug: inbound_count_from_later_notes}``.

    A wikilink can address a note by slug, alias, or filename stem; we
    normalize all three to the candidate slug for counting. Code spans,
    fences, and HTML comments are excluded via
    ``extract_wikilinks(rescue_inline=False)``.
    """
    # Build candidate lookup: slug AND stem both map back to the same
    # canonical slug, so [[birth-signal-design]] matches whether the
    # candidate's slug = "birth-signal-design" or its filename stem is
    # the same. Aliases are also folded in.
    canonical_by_key: dict[str, str] = {}
    candidate_created: dict[str, datetime] = {}
    for note in candidates:
        slug = note["slug"]
        candidate_created[slug] = note["created"]
        canonical_by_key[slug.lower()] = slug
        canonical_by_key[note["path"].stem.lower()] = slug
        aliases = note["fm"].get("aliases") or []
        if isinstance(aliases, str):
            canonical_by_key[aliases.strip().lower()] = slug
        elif isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    canonical_by_key[alias.strip().lower()] = slug

    if not canonical_by_key:
        return {}

    inbound: dict[str, int] = defaultdict(int)
    # Walk every note in the vault — even candidates can link to each
    # other; the "from later" date check is the gate that matters.
    for md in vault_dir.rglob("*.md"):
        rel_parts = md.relative_to(vault_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if md.name in EXCLUDED_NAMES:
            continue
        text = _read_text(md)
        if not text:
            continue
        fm, body = split_frontmatter(text)
        source_created = _parse_created_date(fm.get("created"))
        if source_created is None:
            continue

        # Per-source dedup: multiple links from one source to the same
        # target count once. Matches count_inbound_links semantics.
        targets: set[str] = set()

        body_no_comments = _strip_html_comments(body)
        for raw in extract_wikilinks(body_no_comments, rescue_inline=False):
            target = raw.strip()
            if "#" in target:
                target = target.split("#", 1)[0].strip()
            if "/" in target:
                target = target.rsplit("/", 1)[-1]
            if target:
                targets.add(target.lower())

        # Frontmatter references too — references: [[slug]] is the live
        # vault's primary structural-link surface (PR #516 fix).
        for val in fm.values():
            if isinstance(val, str):
                for raw in extract_wikilinks(val, rescue_inline=False):
                    target = raw.strip()
                    if "#" in target:
                        target = target.split("#", 1)[0].strip()
                    if "/" in target:
                        target = target.rsplit("/", 1)[-1]
                    if target:
                        targets.add(target.lower())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for raw in extract_wikilinks(item, rescue_inline=False):
                            target = raw.strip()
                            if "#" in target:
                                target = target.split("#", 1)[0].strip()
                            if "/" in target:
                                target = target.rsplit("/", 1)[-1]
                            if target:
                                targets.add(target.lower())

        for target_key in targets:
            canonical = canonical_by_key.get(target_key)
            if canonical is None:
                continue
            # "Later" includes same-day — a note created on the same
            # calendar day is post-hoc evidence the target was useful.
            if source_created >= candidate_created[canonical]:
                # But don't credit a note for linking to itself.
                source_slug = _slug_from_fm(fm, md.stem)
                if source_slug == canonical:
                    continue
                inbound[canonical] += 1

    return dict(inbound)


# ---------------------------------------------------------------------------
# Classification


def classify_keywords(
    keywords: list[str],
    investigation: frozenset[str],
    operational: frozenset[str],
) -> str:
    """Categorize a note's trigger_keywords.

    Returns ``"investigation"``, ``"operational"``, or ``"ambiguous"``.

    Rules from spec section "Classification logic":
    1. ``investigation`` if any keyword matches an investigation pattern
    2. ``operational`` if any keyword matches an operational pattern
    3. **Both match → operational** (operational wins; a note that's
       both useful AND was investigated is still useful)
    4. No keywords → ``ambiguous``

    Matching is substring-based on word tokens within each keyword
    string. The live vault has messy keyword phrases like
    ``"phase research"`` and ``"[[2026"`` — substring match against
    cleaned tokens keeps the signal usable without a full NLP pass.
    """
    if not keywords:
        return "ambiguous"

    has_investigation = False
    has_operational = False
    for raw_kw in keywords:
        kw = raw_kw.lower()
        # Direct match.
        if kw in investigation:
            has_investigation = True
        if kw in operational:
            has_operational = True
        # Token match — handles "decay-metrics" matching "decay",
        # "protein tracking" matching "protein", etc.
        tokens = [t for t in kw.replace("-", " ").replace("_", " ").split() if t]
        for tok in tokens:
            if tok in investigation:
                has_investigation = True
            if tok in operational:
                has_operational = True

    if has_operational:
        return "operational"
    if has_investigation:
        return "investigation"
    return "ambiguous"


def classify_note(
    note: dict[str, Any],
    burst_days: set[str],
    investigation: frozenset[str],
    operational: frozenset[str],
    inbound_from_later: dict[str, int],
) -> str:
    """Bucket a note as ``burst_artifact``, ``useful_poorly_linked``,
    or ``ambiguous``.

    Rules (from spec, ordered):

    - **reference/ is always bucket B** (evergreen notes need bridging,
      not archival)
    - **Bucket A (burst_artifact)** requires ALL: burst-day origin,
      investigation keywords, AND no inbound links from later notes
    - **Bucket B (useful_poorly_linked)** if any of: operational
      keywords, has inbound from later, OR not a burst-day note
    - **Ambiguous** is the falls-through case (burst-day origin, no
      inbound, but no clean keyword classification)
    """
    if note["folder"] in _BUCKET_B_FORCED_FOLDERS:
        return "useful_poorly_linked"

    is_burst = note["created"].strftime("%Y-%m-%d") in burst_days
    kw_category = classify_keywords(
        note["trigger_keywords"], investigation, operational
    )
    has_inbound = inbound_from_later.get(note["slug"], 0) > 0

    # Bucket A: strong-signal burst artifact — burst day, clearly
    # investigation-flavored, no evidence later work referenced it.
    if is_burst and kw_category == "investigation" and not has_inbound:
        return "burst_artifact"

    # Bucket B: any positive signal of usefulness.
    if kw_category == "operational" or has_inbound or not is_burst:
        return "useful_poorly_linked"

    # Default — burst day, no inbound, no clean keyword classification.
    return "ambiguous"


# ---------------------------------------------------------------------------
# Top-level scan + event assembly


def compute_birth_signal(
    vault_dir: Path,
    *,
    age_days: int = DEFAULT_ZERO_ACCESS_AGE_DAYS,
    patterns_path: Path | None = None,
    today: datetime | None = None,
) -> dict[str, Any]:
    """End-to-end birth signal pass.

    Returns the payload that goes into the ``birth_signal`` event::

        {
            "total_zero_access_notes": int,    # all zero-access notes (pre-age filter)
            "candidates": int,                  # zero-access AND age >= N
            "burst_artifacts": int,             # bucket A count
            "useful_poorly_linked": int,        # bucket B count
            "ambiguous": int,
            "burst_days": int,                  # observability — number of burst days detected
            "by_bucket": {                      # per-bucket slug lists (capped)
                "burst_artifact": [slug, ...],
                "useful_poorly_linked": [slug, ...],
                "ambiguous": [slug, ...],
            },
            "archived_last_cycle": 0,           # action wiring is downstream — always 0 here
            "bridged_last_cycle": 0,
        }

    The ``archived_last_cycle`` / ``bridged_last_cycle`` fields are
    present (per spec) but always 0: this module is observability-only.
    A future PR that wires the archival / bridge actions will fill
    them in.
    """
    if today is None:
        today = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    investigation, operational = load_pattern_library(patterns_path)

    # Pre-age count: zero-access notes regardless of age. Useful for
    # correlation with vault_health's access_decay (which uses 5d).
    total_zero_access = 0
    for md in _iter_candidate_notes(vault_dir):
        text = _read_text(md)
        if not text:
            continue
        fm, _body = split_frontmatter(text)
        if not fm or _note_type_is_daily(fm):
            continue
        ac = _parse_access_count(fm.get("access_count"))
        if ac == 0:
            total_zero_access += 1

    candidates = detect_zero_access_notes(
        vault_dir, age_days=age_days, today=today,
    )
    burst_days = compute_burst_days(candidates)
    inbound_from_later = compute_inbound_from_later(vault_dir, candidates)

    by_bucket: dict[str, list[str]] = {
        "burst_artifact": [],
        "useful_poorly_linked": [],
        "ambiguous": [],
    }
    for note in candidates:
        bucket = classify_note(
            note, burst_days, investigation, operational, inbound_from_later,
        )
        by_bucket[bucket].append(note["slug"])

    for slugs in by_bucket.values():
        slugs.sort()

    return {
        "total_zero_access_notes": total_zero_access,
        "candidates": len(candidates),
        "burst_artifacts": len(by_bucket["burst_artifact"]),
        "useful_poorly_linked": len(by_bucket["useful_poorly_linked"]),
        "ambiguous": len(by_bucket["ambiguous"]),
        "burst_days": len(burst_days),
        "by_bucket": by_bucket,
        "archived_last_cycle": 0,
        "bridged_last_cycle": 0,
    }


def build_birth_signal_event(
    vault_dir: Path,
    *,
    age_days: int = DEFAULT_ZERO_ACCESS_AGE_DAYS,
    patterns_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the ``birth_signal`` event for ``events.jsonl``.

    Mirrors ``vault_health.build_vault_health_event`` — ISO8601
    timestamp with local offset, ``date`` (YYYY-MM-DD) and ``time``
    (HH:MM TZNAME) fields for human-readable correlation.

    The ``by_bucket`` slug lists are dropped from the emitted event to
    keep ``events.jsonl`` lines reasonably small. Callers that want the
    slug breakdown should consume the dict from
    :func:`compute_birth_signal` directly (the CLI dry-run path does).
    """
    if now is None:
        now = _local_now()

    try:
        local_offset = datetime.now(timezone.utc).astimezone().strftime("%z")
        if len(local_offset) == 5:
            local_offset = f"{local_offset[:3]}:{local_offset[3:]}"
    except Exception:  # noqa: BLE001
        local_offset = ""
    ts = now.strftime("%Y-%m-%dT%H:%M:%S") + local_offset
    tzname = datetime.now().astimezone().tzname() or ""
    time_str = f"{now.strftime('%H:%M')} {tzname}".strip()

    payload = compute_birth_signal(
        vault_dir,
        age_days=age_days,
        patterns_path=patterns_path,
        today=now,
    )

    event = {
        "ts": ts,
        "type": "birth_signal",
        "date": now.strftime("%Y-%m-%d"),
        "time": time_str,
        "total_zero_access_notes": payload["total_zero_access_notes"],
        "candidates": payload["candidates"],
        "burst_artifacts": payload["burst_artifacts"],
        "useful_poorly_linked": payload["useful_poorly_linked"],
        "ambiguous": payload["ambiguous"],
        "burst_days": payload["burst_days"],
        "archived_last_cycle": payload["archived_last_cycle"],
        "bridged_last_cycle": payload["bridged_last_cycle"],
    }
    return event


def birth_signal_event_exists_for_date(
    events_path: Path, date_str: str,
) -> bool:
    """True iff ``events.jsonl`` already has a ``birth_signal`` event
    whose ``date`` field equals ``date_str``."""
    if not events_path.exists():
        return False
    try:
        with events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    evt.get("type") == "birth_signal"
                    and evt.get("date") == date_str
                ):
                    return True
    except OSError:
        return False
    return False


def _append_event(events_path: Path, event: dict[str, Any]) -> None:
    """Append ``event`` as a single JSON line. Creates parent dir."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event)
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="metrics.birth_signal",
        description=(
            "Detect and classify zero-access vault notes (the 'birth → "
            "zero' path that count_access_decay misses). Observability "
            "only — no archival, no bridge action."
        ),
    )
    parser.add_argument(
        "--vault",
        required=True,
        type=Path,
        help="Path to the cortex-memory vault directory.",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=None,
        help=(
            "Path to memory/events.jsonl. Required unless --dry-run is set."
        ),
    )
    parser.add_argument(
        "--patterns",
        type=Path,
        default=None,
        help=(
            "Path to the keyword pattern library YAML. Defaults to in-"
            "module defaults when unset or missing."
        ),
    )
    parser.add_argument(
        "--age-days",
        type=int,
        default=DEFAULT_ZERO_ACCESS_AGE_DAYS,
        help=(
            "Minimum age (days) before a zero-access note is a candidate. "
            f"Default {DEFAULT_ZERO_ACCESS_AGE_DAYS} (spec-validated)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the full payload (including per-bucket slug lists) "
            "and exit. Nothing is written to --events."
        ),
    )
    parser.add_argument(
        "--check-existing",
        action="store_true",
        help=(
            "When --events is set, skip writing if today already has a "
            "birth_signal event on disk. Matches the vault_health "
            "morning-scan dedup behavior."
        ),
    )
    args = parser.parse_args(argv)

    now = _local_now()

    if args.dry_run:
        payload = compute_birth_signal(
            args.vault,
            age_days=args.age_days,
            patterns_path=args.patterns,
            today=now,
        )
        # Truncate per-bucket lists for readability — 1400+ slugs is
        # unmanageable in a terminal dump.
        truncated = dict(payload)
        truncated["by_bucket"] = {
            bucket: slugs[:20] for bucket, slugs in payload["by_bucket"].items()
        }
        truncated["by_bucket_truncated"] = {
            bucket: max(0, len(slugs) - 20)
            for bucket, slugs in payload["by_bucket"].items()
        }
        print(json.dumps(truncated, indent=2, sort_keys=True))
        return 0

    if args.events is None:
        parser.error("--events is required unless --dry-run is set")

    today_str = now.strftime("%Y-%m-%d")
    if args.check_existing and birth_signal_event_exists_for_date(
        args.events, today_str,
    ):
        # Already wrote today's event — silent no-op so the daily scan
        # can call this unconditionally.
        return 0

    event = build_birth_signal_event(
        args.vault,
        age_days=args.age_days,
        patterns_path=args.patterns,
        now=now,
    )
    _append_event(args.events, event)
    print(json.dumps(event))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
