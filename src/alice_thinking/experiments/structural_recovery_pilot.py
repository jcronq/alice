"""Structural recovery P0 pilot — corrected-spec implementation.

Source of truth
---------------

``~/alice-mind/cortex-memory/research/2026-06-29-structural-recovery-pilot-corrected-spec.md``

This script is the standalone executor for the P0 pilot. It samples a
treatment + control group of low-access research notes, computes
hub-link targets via PageRank-weighted TF-IDF cosine similarity over
``reference/`` + ``projects/``, optionally injects the wikilinks, and
records baseline / follow-up metrics for the acceptance verdict.

The five subcommands map to the experiment lifecycle::

    --dry-run        — sample groups + compute targets, print only (no writes)
    --execute        — actually insert wikilinks into treatment notes
    --measure        — snapshot baseline metrics (pre-intervention)
    --final-measure  — snapshot follow-up metrics + print acceptance verdict
    --rollback <log> — reverse insertions from a transaction log

**Merge gate:** ``--execute`` is the only subcommand that mutates the
vault. The expectation is that this script is reviewed and merged, then
``--execute`` is run inside a supervised window. The other four
subcommands are read-only (``--rollback`` writes, but only against a
prior ``--execute`` transaction log).

Pattern references
------------------

- File read / frontmatter / atomic write pattern follows
  ``alice_thinking.memory_worker.correction_cascade_auto_propagate``.
- ``split_frontmatter`` comes from ``indexer.yaml_lite`` (verbatim — no
  reinvented frontmatter parser).
- ``compute_pagerank`` comes from ``metrics.pagerank_metric``.

Implementation notes
--------------------

- TF-IDF is pure-Python (no sklearn / scipy / numpy). The vault doesn't
  ship those deps and the corpus (~3k notes, ~400 targets) is small
  enough that a dict-based implementation is sub-second.
- Random sampling uses a fixed seed (``42``) for reproducibility.
- All state writes go under ``~/alice-mind/inner/state/`` per the
  Speaking↔thinking shared-state convention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import pathlib
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from indexer.yaml_lite import split_frontmatter
from metrics.pagerank_metric import compute_pagerank

logger = logging.getLogger(__name__)

# --- Constants --------------------------------------------------------

#: Random seed for reproducible group selection.
_RANDOM_SEED = 42

#: N=50 treatment + N=50 control per the spec.
_GROUP_SIZE = 50

#: Worst-performing domains (per the spec). We require at least
#: ``_MIN_WORST_DOMAIN`` notes from these in the treatment group.
_WORST_DOMAINS = ("alice-architecture", "memory-design", "alice-thinking")
_MIN_WORST_DOMAIN = 15

#: Hub-link injection parameters (from the spec).
_LINKS_PER_NOTE = 3
_OVERLAP_THRESHOLD = 0.25
_TARGET_FOLDERS = ("reference", "projects")

#: Candidate pool filter — research notes with low access only.
_CANDIDATE_FOLDER = "research"
_CANDIDATE_ACCESS_MAX = 3

#: Acceptance thresholds (from the spec).
_PRIMARY_ACCESS_DELTA_PCT = 0.15  # ≥ 15% access_count delta
_SECONDARY_INBOUND_DELTA = 0.5    # ≥ 0.5 more inbound links per note

#: Timezone for event timestamps (events.jsonl convention).
_EDT = ZoneInfo("America/New_York")

#: Marker line for the inserted hub-link section. Used by ``--rollback``
#: to recognise its own edits. Kept short to avoid visual noise in the
#: target note.
_INSERT_MARKER = "<!-- structural-recovery-pilot:p0 -->"


# --- Path resolution --------------------------------------------------


def _mind_root() -> pathlib.Path:
    """Return the alice-mind root (``ALICE_MIND`` env or ``~/alice-mind``)."""
    raw = os.environ.get("ALICE_MIND")
    return pathlib.Path(raw) if raw else pathlib.Path.home() / "alice-mind"


def _vault_root(mind: pathlib.Path) -> pathlib.Path:
    return mind / "cortex-memory"


def _index_db(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "state" / "cortex-index.db"


def _state_dir(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "state"


def _timestamp() -> str:
    """Compact UTC timestamp for filenames."""
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


# --- DB queries -------------------------------------------------------


def _verify_schema(db_path: pathlib.Path) -> None:
    """STOP-condition check: confirm tables/columns the script depends on exist."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing_tables = {"notes", "note_metrics", "links"} - tables
        if missing_tables:
            raise RuntimeError(
                f"cortex-index.db missing required tables: {sorted(missing_tables)}"
            )
        notes_cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
        for col in ("slug", "path", "folder", "title", "created"):
            if col not in notes_cols:
                raise RuntimeError(f"notes table missing column: {col}")
        metric_cols = {r[1] for r in conn.execute("PRAGMA table_info(note_metrics)")}
        if "access_count" not in metric_cols:
            raise RuntimeError("note_metrics table missing column: access_count")
        link_cols = {r[1] for r in conn.execute("PRAGMA table_info(links)")}
        for col in ("source_slug", "target_slug"):
            if col not in link_cols:
                raise RuntimeError(f"links table missing column: {col}")
    finally:
        conn.close()


def _query_candidates(db_path: pathlib.Path) -> list[dict[str, Any]]:
    """Return all research notes with access_count <= 3.

    Returns rows of ``{slug, title, folder, path, access_count, created}``.
    Domain is intentionally NOT set here — the spec notes that ``folder``
    is "research" for all rows, and real domain lives in frontmatter.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT n.slug, n.title, n.folder, n.path, n.created, m.access_count
            FROM notes n
            JOIN note_metrics m ON m.slug = n.slug
            WHERE n.folder = ?
              AND m.access_count <= ?
            ORDER BY m.access_count ASC, n.created DESC
            """,
            (_CANDIDATE_FOLDER, _CANDIDATE_ACCESS_MAX),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "slug": r[0],
            "title": r[1],
            "folder": r[2],
            "path": r[3],
            "created": r[4],
            "access_count": r[5],
        }
        for r in rows
    ]


def _query_target_notes(db_path: pathlib.Path) -> list[dict[str, Any]]:
    """Return all reference/ + projects/ notes (the hub-target pool)."""
    conn = sqlite3.connect(str(db_path))
    try:
        placeholders = ",".join("?" * len(_TARGET_FOLDERS))
        rows = conn.execute(
            f"""
            SELECT slug, title, path, body
            FROM notes
            WHERE folder IN ({placeholders})
            """,
            _TARGET_FOLDERS,
        ).fetchall()
    finally:
        conn.close()
    return [
        {"slug": r[0], "title": r[1], "path": r[2], "body": r[3] or ""}
        for r in rows
    ]


def _query_access_counts(db_path: pathlib.Path, slugs: list[str]) -> dict[str, int]:
    """Return ``{slug: access_count}`` for the given slugs."""
    if not slugs:
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        out: dict[str, int] = {}
        chunk = 500
        for i in range(0, len(slugs), chunk):
            sub = slugs[i : i + chunk]
            placeholders = ",".join("?" * len(sub))
            rows = conn.execute(
                f"SELECT slug, access_count FROM note_metrics WHERE slug IN ({placeholders})",
                sub,
            ).fetchall()
            for slug, count in rows:
                out[slug] = count
        # Missing slugs default to 0 (no recorded access).
        for slug in slugs:
            out.setdefault(slug, 0)
        return out
    finally:
        conn.close()


def _query_inbound_link_counts(db_path: pathlib.Path, slugs: list[str]) -> dict[str, int]:
    """Return ``{slug: inbound_link_count}`` for the given slugs.

    Counted as ``SELECT COUNT(*) FROM links WHERE target_slug = ?`` —
    every recorded link counts toward inbound, structural or not. The
    spec's secondary metric measures whether other notes start
    referencing treatment notes; we don't filter by ``is_structural``
    because we want any new reference to register.
    """
    if not slugs:
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        out: dict[str, int] = {}
        chunk = 500
        for i in range(0, len(slugs), chunk):
            sub = slugs[i : i + chunk]
            placeholders = ",".join("?" * len(sub))
            rows = conn.execute(
                f"""
                SELECT target_slug, COUNT(*)
                FROM links
                WHERE target_slug IN ({placeholders})
                GROUP BY target_slug
                """,
                sub,
            ).fetchall()
            for slug, count in rows:
                out[slug] = count
        for slug in slugs:
            out.setdefault(slug, 0)
        return out
    finally:
        conn.close()


# --- Frontmatter / domain extraction ---------------------------------


def _read_note_text(vault: pathlib.Path, path: str) -> str:
    full = vault / path
    return full.read_text(encoding="utf-8")


def _extract_domain(vault: pathlib.Path, path: str) -> str:
    """Read the note's frontmatter and return ``domain`` (or empty string)."""
    try:
        text = _read_note_text(vault, path)
    except OSError:
        return ""
    fm, _body = split_frontmatter(text)
    raw = fm.get("domain", "")
    return str(raw).strip() if raw else ""


def _annotate_with_domain(
    candidates: list[dict[str, Any]],
    vault: pathlib.Path,
) -> list[dict[str, Any]]:
    """Mutate each candidate in-place adding ``domain`` from frontmatter."""
    for c in candidates:
        c["domain"] = _extract_domain(vault, c["path"])
    return candidates


def _creation_month(created: str) -> str:
    """Return ``YYYY-MM`` slice of an ISO date string (best effort)."""
    if not created:
        return ""
    return str(created)[:7]


# --- Group selection -------------------------------------------------


def _select_groups(
    candidates: list[dict[str, Any]],
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stratified treatment + matched control selection.

    Treatment: at least ``_MIN_WORST_DOMAIN`` from the worst-performing
    domains, the rest sampled proportionally from the remaining pool.
    Control: matched by (domain, access_count ±1, creation_month),
    sampled from candidates NOT in treatment.
    """
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_domain[c.get("domain") or "_unknown"].append(c)

    worst_pool: list[dict[str, Any]] = []
    for d in _WORST_DOMAINS:
        worst_pool.extend(by_domain.get(d, []))
    other_pool: list[dict[str, Any]] = [
        c for c in candidates if (c.get("domain") or "_unknown") not in _WORST_DOMAINS
    ]

    treatment: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    worst_take = min(_MIN_WORST_DOMAIN, len(worst_pool))
    if worst_take > 0:
        picked = rng.sample(worst_pool, worst_take)
        for c in picked:
            treatment.append(c)
            seen_slugs.add(c["slug"])

    remaining_needed = _GROUP_SIZE - len(treatment)
    if remaining_needed > 0:
        # Sample from everything not yet picked (other_pool + leftover worst).
        leftover = [
            c
            for c in (worst_pool + other_pool)
            if c["slug"] not in seen_slugs
        ]
        # Dedup leftover by slug — worst_pool + other_pool are disjoint by
        # construction, but be defensive.
        uniq: dict[str, dict[str, Any]] = {}
        for c in leftover:
            uniq.setdefault(c["slug"], c)
        leftover = list(uniq.values())
        take = min(remaining_needed, len(leftover))
        picked = rng.sample(leftover, take)
        for c in picked:
            treatment.append(c)
            seen_slugs.add(c["slug"])

    # Build control pool — everything not in treatment.
    control_pool = [c for c in candidates if c["slug"] not in seen_slugs]
    # Pre-index control by (domain, ac, month).
    by_key: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for c in control_pool:
        key = (
            c.get("domain") or "_unknown",
            int(c.get("access_count", 0)),
            _creation_month(c.get("created", "")),
        )
        by_key[key].append(c)

    control: list[dict[str, Any]] = []
    control_used: set[str] = set()
    for t in treatment:
        domain = t.get("domain") or "_unknown"
        ac = int(t.get("access_count", 0))
        month = _creation_month(t.get("created", ""))
        match: Optional[dict[str, Any]] = None
        # Try exact match first, then widen access_count ±1, then drop month.
        for ac_delta in (0, 1, -1):
            cand_list = by_key.get((domain, ac + ac_delta, month), [])
            cand_list = [c for c in cand_list if c["slug"] not in control_used]
            if cand_list:
                match = rng.choice(cand_list)
                break
        if match is None:
            # Drop month constraint.
            for ac_delta in (0, 1, -1):
                cand_list = [
                    c
                    for c in control_pool
                    if c["slug"] not in control_used
                    and (c.get("domain") or "_unknown") == domain
                    and int(c.get("access_count", 0)) == ac + ac_delta
                ]
                if cand_list:
                    match = rng.choice(cand_list)
                    break
        if match is None:
            # Final fallback: any unused candidate.
            remaining = [c for c in control_pool if c["slug"] not in control_used]
            if remaining:
                match = rng.choice(remaining)
        if match is not None:
            control.append(match)
            control_used.add(match["slug"])

    return treatment, control


# --- TF-IDF (pure Python) --------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + simple word split. Good enough for cosine similarity."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _strip_frontmatter(text: str) -> str:
    """Return body text without YAML frontmatter (for treatment notes)."""
    _, body = split_frontmatter(text)
    return body


def _compute_idf(corpus_tokens: list[list[str]]) -> dict[str, float]:
    """Standard IDF: log(N / df). Smoothed by adding 1 to df."""
    n = len(corpus_tokens)
    df: Counter[str] = Counter()
    for toks in corpus_tokens:
        for term in set(toks):
            df[term] += 1
    return {term: math.log((n + 1) / (df_t + 1)) + 1 for term, df_t in df.items()}


def _tfidf_vector(
    tokens: list[str],
    idf: dict[str, float],
) -> dict[str, float]:
    """Return ``{term: tfidf}`` for one document."""
    if not tokens:
        return {}
    tf: Counter[str] = Counter(tokens)
    total = sum(tf.values())
    return {
        term: (count / total) * idf.get(term, 0.0)
        for term, count in tf.items()
        if idf.get(term, 0.0) > 0.0
    }


def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    if not v1 or not v2:
        return 0.0
    # Iterate the smaller dict for the dot product.
    small, large = (v1, v2) if len(v1) <= len(v2) else (v2, v1)
    dot = sum(w * large.get(term, 0.0) for term, w in small.items())
    norm1 = math.sqrt(sum(w * w for w in v1.values()))
    norm2 = math.sqrt(sum(w * w for w in v2.values()))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


# --- Target selection ------------------------------------------------


def _select_targets(
    note: dict[str, Any],
    vault: pathlib.Path,
    targets: list[dict[str, Any]],
    target_vectors: list[dict[str, float]],
    pagerank: dict[str, float],
    idf: dict[str, float],
) -> list[dict[str, Any]]:
    """Return up to ``_LINKS_PER_NOTE`` chosen targets for one treatment note.

    Each entry is ``{slug, title, overlap, pagerank, score}``.
    Filters to overlap >= ``_OVERLAP_THRESHOLD`` and ranks by
    ``overlap * pagerank``. Returns ``[]`` if no target meets the
    threshold.
    """
    try:
        text = _read_note_text(vault, note["path"])
    except OSError as exc:
        logger.warning("could not read %s: %s", note["path"], exc)
        return []
    body = _strip_frontmatter(text)
    source_doc = f"{note.get('title', '')}\n{body}"
    source_vec = _tfidf_vector(_tokenize(source_doc), idf)
    if not source_vec:
        return []

    scored: list[dict[str, Any]] = []
    for target, tvec in zip(targets, target_vectors):
        if target["slug"] == note["slug"]:
            continue
        overlap = _cosine(source_vec, tvec)
        if overlap < _OVERLAP_THRESHOLD:
            continue
        pr = pagerank.get(target["slug"], 0.0)
        score = overlap * pr
        scored.append(
            {
                "slug": target["slug"],
                "title": target["title"],
                "overlap": overlap,
                "pagerank": pr,
                "score": score,
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:_LINKS_PER_NOTE]


# --- Wikilink injection ----------------------------------------------


def _bump_updated(fm: dict, body: str) -> str:
    """Render frontmatter + body, bumping ``updated``.

    Mirrors ``correction_cascade_auto_propagate._write_updated_frontmatter``.
    """
    fm["updated"] = time.strftime("%Y-%m-%d %H:%M EDT")
    out = "---\n"
    for k, v in fm.items():
        if isinstance(v, list):
            out += f"{k}:\n"
            for item in v:
                out += f"  - {item}\n"
        else:
            out += f"{k}: {v}\n"
    out += "---\n\n"
    out += body
    return out


def _build_insert_block(targets: list[dict[str, Any]]) -> str:
    """Build the bullet block of wikilinks to inject.

    Uses ``_INSERT_MARKER`` on its own line so ``--rollback`` can locate
    its own edits unambiguously.
    """
    lines = [_INSERT_MARKER]
    for t in targets:
        lines.append(f"- [[{t['slug']}|{t['title']}]]")
    return "\n".join(lines) + "\n"


def _insert_into_body(body: str, block: str) -> tuple[str, int]:
    """Insert ``block`` into ``body`` before any ``## Related`` section.

    If no Related section exists, the block is appended at the end. The
    block is preceded by ``\\n## Related\\n\\n`` when no such header is
    present, so the inserted lines are well-formed markdown.

    Returns ``(new_body, byte_offset_of_insertion_in_new_body)`` —
    the offset is the byte position where the block starts in
    ``new_body``, which is the recovery anchor for ``--rollback``.
    """
    related_re = re.compile(r"^## Related\b.*$", re.MULTILINE)
    match = related_re.search(body)
    if match:
        # Insert block directly after the Related header line.
        line_end = body.find("\n", match.end())
        if line_end == -1:
            line_end = len(body)
        else:
            line_end += 1  # past the newline
        new_body = body[:line_end] + block + body[line_end:]
        offset = line_end
    else:
        suffix = body.rstrip()
        if not suffix.endswith("\n"):
            suffix += "\n"
        prefix = suffix + "\n## Related\n\n"
        new_body = prefix + block
        offset = len(prefix)
    return new_body, offset


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Subcommand: dry-run ---------------------------------------------


def _print_group(label: str, notes: list[dict[str, Any]]) -> None:
    print(f"\n{label} (n={len(notes)}):")
    print(f"  {'slug':70s} access  domain                    title")
    for n in notes:
        slug = n["slug"][:68]
        domain = (n.get("domain") or "_unknown")[:24]
        title = (n.get("title") or "")[:60]
        print(f"  {slug:70s} {int(n.get('access_count', 0)):>5d}  {domain:24s}  {title}")


def cmd_dry_run(mind: pathlib.Path) -> int:
    """Print group selection + chosen targets. No writes."""
    db = _index_db(mind)
    vault = _vault_root(mind)
    _verify_schema(db)

    print(f"[dry-run] mind={mind} db={db}")

    candidates = _query_candidates(db)
    print(f"[dry-run] {len(candidates)} research notes with access_count <= {_CANDIDATE_ACCESS_MAX}")

    print("[dry-run] reading frontmatter for domain annotation...")
    _annotate_with_domain(candidates, vault)

    rng = random.Random(_RANDOM_SEED)
    treatment, control = _select_groups(candidates, rng)
    _print_group("TREATMENT", treatment)
    _print_group("CONTROL", control)

    print("\n[dry-run] loading target pool (reference/ + projects/)...")
    targets = _query_target_notes(db)
    print(f"[dry-run] {len(targets)} target notes loaded")

    print("[dry-run] computing TF-IDF corpus + PageRank...")
    target_token_lists = [
        _tokenize(f"{t['title']}\n{t['body']}") for t in targets
    ]
    idf = _compute_idf(target_token_lists)
    target_vectors = [_tfidf_vector(toks, idf) for toks in target_token_lists]
    pagerank = compute_pagerank(db)

    print("\n[dry-run] hub-link target selection per treatment note:")
    n_with_targets = 0
    n_skipped = 0
    for note in treatment:
        chosen = _select_targets(note, vault, targets, target_vectors, pagerank, idf)
        if chosen:
            n_with_targets += 1
            print(f"  + [[{note['slug']}]] -> {len(chosen)} target(s):")
            for t in chosen:
                print(
                    f"      {t['slug']:60s} overlap={t['overlap']:.3f} "
                    f"pr={t['pagerank']:.6f} score={t['score']:.6f}"
                )
        else:
            n_skipped += 1
            print(f"  - [[{note['slug']}]] -> SKIP (no target meets overlap>={_OVERLAP_THRESHOLD})")

    print(
        f"\n[dry-run] summary: {n_with_targets} treatment notes have >=1 valid target, "
        f"{n_skipped} skipped"
    )
    return 0


# --- Subcommand: execute ---------------------------------------------


def cmd_execute(mind: pathlib.Path) -> int:
    """Insert wikilinks into treatment notes and write transaction log."""
    # Lazy import: vault_lock is only meaningful for write paths, and the
    # import requires the alice-thinking package to be installed.
    from alice_thinking import vault_lock

    db = _index_db(mind)
    vault = _vault_root(mind)
    _verify_schema(db)

    candidates = _query_candidates(db)
    _annotate_with_domain(candidates, vault)
    rng = random.Random(_RANDOM_SEED)
    treatment, control = _select_groups(candidates, rng)

    targets = _query_target_notes(db)
    target_token_lists = [_tokenize(f"{t['title']}\n{t['body']}") for t in targets]
    idf = _compute_idf(target_token_lists)
    target_vectors = [_tfidf_vector(toks, idf) for toks in target_token_lists]
    pagerank = compute_pagerank(db)

    state_dir = _state_dir(mind)
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = _timestamp()
    txn_path = state_dir / f"pilot-transaction-{ts}.jsonl"

    # Persist group membership alongside the transaction log so
    # --measure / --final-measure can find the same groups without
    # re-running selection (random.sample is deterministic for the same
    # seed + candidate ordering, but the candidate ordering is sensitive
    # to DB rebuilds — better to lock the assignment to the log).
    groups_path = state_dir / f"pilot-groups-{ts}.json"
    with open(groups_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": ts,
                "seed": _RANDOM_SEED,
                "treatment": [n["slug"] for n in treatment],
                "control": [n["slug"] for n in control],
            },
            f,
            indent=2,
        )
    print(f"[execute] group assignment -> {groups_path}")

    written = 0
    skipped = 0
    with open(txn_path, "a", encoding="utf-8") as txn_log:
        for note in treatment:
            chosen = _select_targets(note, vault, targets, target_vectors, pagerank, idf)
            if not chosen:
                skipped += 1
                continue
            note_path = vault / note["path"]
            original_text = note_path.read_text(encoding="utf-8")
            original_hash = _hash_text(original_text)
            fm, body = split_frontmatter(original_text)
            block = _build_insert_block(chosen)
            # Idempotency: if the marker already appears, skip.
            if _INSERT_MARKER in body:
                skipped += 1
                logger.info("already injected: %s", note["slug"])
                continue
            new_body, byte_offset = _insert_into_body(body, block)
            new_text = _bump_updated(fm, new_body)

            with vault_lock.acquire(note_path, mode=vault_lock.LockMode.EXCLUSIVE):
                note_path.write_text(new_text, encoding="utf-8")
            written += 1

            entry = {
                "ts": datetime.now(_EDT).isoformat(),
                "source_slug": note["slug"],
                "source_path": note["path"],
                "original_hash": original_hash,
                "new_hash": _hash_text(new_text),
                "insert_byte_offset_in_body": byte_offset,
                "block": block,
                "targets": chosen,
            }
            txn_log.write(json.dumps(entry) + "\n")

    print(f"[execute] wrote {written} notes, skipped {skipped}")
    print(f"[execute] transaction log -> {txn_path}")
    return 0


# --- Subcommands: measure / final-measure ----------------------------


def _latest_groups_file(state_dir: pathlib.Path) -> Optional[pathlib.Path]:
    files = sorted(state_dir.glob("pilot-groups-*.json"))
    return files[-1] if files else None


def _load_groups(mind: pathlib.Path) -> tuple[list[str], list[str]]:
    """Load the most recent pilot group assignment. Falls back to recomputing.

    Recomputation uses the same seed + selection, but since the DB state
    can drift the assignment-file path is preferred. If no groups file
    exists yet (e.g. measure-before-execute), recompute and emit a
    warning.
    """
    state_dir = _state_dir(mind)
    groups_file = _latest_groups_file(state_dir)
    if groups_file is not None:
        with open(groups_file, encoding="utf-8") as f:
            data = json.load(f)
        return data["treatment"], data["control"]
    logger.warning("no pilot-groups-*.json found, recomputing from current DB state")
    db = _index_db(mind)
    vault = _vault_root(mind)
    candidates = _query_candidates(db)
    _annotate_with_domain(candidates, vault)
    rng = random.Random(_RANDOM_SEED)
    treatment, control = _select_groups(candidates, rng)
    return [n["slug"] for n in treatment], [n["slug"] for n in control]


def _snapshot(mind: pathlib.Path, label: str) -> dict[str, Any]:
    db = _index_db(mind)
    _verify_schema(db)
    treatment, control = _load_groups(mind)
    access = _query_access_counts(db, treatment + control)
    inbound = _query_inbound_link_counts(db, treatment + control)
    return {
        "label": label,
        "timestamp": _timestamp(),
        "treatment": {
            "slugs": treatment,
            "access_count": {s: access[s] for s in treatment},
            "inbound_link_count": {s: inbound[s] for s in treatment},
        },
        "control": {
            "slugs": control,
            "access_count": {s: access[s] for s in control},
            "inbound_link_count": {s: inbound[s] for s in control},
        },
    }


def cmd_measure(mind: pathlib.Path) -> int:
    """Snapshot baseline access_count + inbound link count."""
    snap = _snapshot(mind, "baseline")
    state_dir = _state_dir(mind)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"pilot-baseline-{snap['timestamp']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"[measure] baseline snapshot -> {path}")
    return 0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _latest_baseline(state_dir: pathlib.Path) -> Optional[pathlib.Path]:
    files = sorted(state_dir.glob("pilot-baseline-*.json"))
    return files[-1] if files else None


def cmd_final_measure(mind: pathlib.Path) -> int:
    """Snapshot follow-up metrics + print acceptance verdict."""
    state_dir = _state_dir(mind)
    baseline_path = _latest_baseline(state_dir)
    if baseline_path is None:
        print("[final-measure] ERROR: no pilot-baseline-*.json found; run --measure first")
        return 2
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)

    snap = _snapshot(mind, "final")
    final_path = state_dir / f"pilot-final-{snap['timestamp']}.json"
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2)
    print(f"[final-measure] final snapshot -> {final_path}")
    print(f"[final-measure] compared against baseline {baseline_path}")

    def _deltas(group: str) -> dict[str, float]:
        b_access = baseline[group]["access_count"]
        f_access = snap[group]["access_count"]
        b_inbound = baseline[group]["inbound_link_count"]
        f_inbound = snap[group]["inbound_link_count"]
        slugs = baseline[group]["slugs"]
        access_b_total = sum(b_access.get(s, 0) for s in slugs)
        access_f_total = sum(f_access.get(s, 0) for s in slugs)
        inbound_b_mean = _mean([float(b_inbound.get(s, 0)) for s in slugs])
        inbound_f_mean = _mean([float(f_inbound.get(s, 0)) for s in slugs])
        return {
            "access_baseline_sum": access_b_total,
            "access_final_sum": access_f_total,
            "access_delta_abs": access_f_total - access_b_total,
            "inbound_baseline_mean": inbound_b_mean,
            "inbound_final_mean": inbound_f_mean,
            "inbound_delta_mean": inbound_f_mean - inbound_b_mean,
        }

    t = _deltas("treatment")
    c = _deltas("control")

    # Primary acceptance: treatment access_count growth ≥ 15% over control.
    # Compute relative delta from baseline; guard against zero baseline.
    def _rel_growth(d: dict[str, float]) -> float:
        base = d["access_baseline_sum"]
        return (d["access_delta_abs"] / base) if base > 0 else 0.0

    treatment_growth = _rel_growth(t)
    control_growth = _rel_growth(c)
    primary_delta = treatment_growth - control_growth
    primary_pass = primary_delta >= _PRIMARY_ACCESS_DELTA_PCT

    # Secondary acceptance: treatment inbound mean growth ≥ control + 0.5.
    secondary_delta = t["inbound_delta_mean"] - c["inbound_delta_mean"]
    secondary_pass = secondary_delta >= _SECONDARY_INBOUND_DELTA

    print("\n[final-measure] === RESULTS ===")
    print(f"  treatment access growth: {treatment_growth:.2%}  (+{t['access_delta_abs']} abs)")
    print(f"  control   access growth: {control_growth:.2%}  (+{c['access_delta_abs']} abs)")
    print(f"  primary  delta (T - C):  {primary_delta:.2%}   "
          f"(threshold >= {_PRIMARY_ACCESS_DELTA_PCT:.0%}) "
          f"-> {'PASS' if primary_pass else 'FAIL'}")
    print(f"  treatment inbound mean delta: {t['inbound_delta_mean']:+.3f}")
    print(f"  control   inbound mean delta: {c['inbound_delta_mean']:+.3f}")
    print(f"  secondary delta (T - C):      {secondary_delta:+.3f}    "
          f"(threshold >= +{_SECONDARY_INBOUND_DELTA}) "
          f"-> {'PASS' if secondary_pass else 'FAIL'}")
    print(f"\n[final-measure] VERDICT: "
          f"primary={'PASS' if primary_pass else 'FAIL'}, "
          f"secondary={'PASS' if secondary_pass else 'FAIL'}")
    return 0


# --- Subcommand: rollback --------------------------------------------


def cmd_rollback(mind: pathlib.Path, txn_path: pathlib.Path) -> int:
    """Reverse insertions recorded in a transaction log.

    Strategy: for each entry, re-read the note, locate the inserted
    block by ``_INSERT_MARKER``, confirm the surrounding text matches
    the recorded block, and excise. If the block isn't where we expect,
    log a warning and SKIP rather than damaging the file.
    """
    from alice_thinking import vault_lock

    vault = _vault_root(mind)
    if not txn_path.exists():
        print(f"[rollback] ERROR: transaction log not found: {txn_path}")
        return 2

    reversed_count = 0
    skipped = 0
    with open(txn_path, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]

    for entry in entries:
        source_path = vault / entry["source_path"]
        if not source_path.exists():
            logger.warning("rollback: note missing: %s", source_path)
            skipped += 1
            continue
        text = source_path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        block: str = entry["block"]
        if block not in body:
            logger.warning(
                "rollback: block not found in %s — skipping (manual recovery needed)",
                entry["source_slug"],
            )
            skipped += 1
            continue
        new_body = body.replace(block, "", 1)
        # Also drop the synthetic "## Related" header we may have added.
        # Pattern: trailing whitespace + "\n## Related\n\n" left dangling
        # with nothing under it. Be conservative: only strip if Related
        # is empty (nothing but whitespace until EOF or next header).
        new_body = re.sub(
            r"\n## Related\s*\n\s*(?=\Z|\n## )",
            "\n",
            new_body,
        )
        new_text = _bump_updated(fm, new_body)
        with vault_lock.acquire(source_path, mode=vault_lock.LockMode.EXCLUSIVE):
            source_path.write_text(new_text, encoding="utf-8")
        reversed_count += 1

    print(f"[rollback] reversed {reversed_count} insertions, skipped {skipped}")
    return 0


# --- CLI -------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="structural_recovery_pilot",
        description="Structural recovery P0 pilot — corrected-spec implementation.",
    )
    p.add_argument(
        "--mind",
        type=pathlib.Path,
        default=None,
        help="alice-mind root (default: $ALICE_MIND or ~/alice-mind)",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Print group selection + chosen targets. No writes.")
    group.add_argument("--execute", action="store_true",
                       help="Insert wikilinks into treatment notes (vault-write).")
    group.add_argument("--measure", action="store_true",
                       help="Snapshot baseline access_count + inbound link count.")
    group.add_argument("--final-measure", action="store_true",
                       help="Snapshot follow-up metrics + acceptance verdict.")
    group.add_argument("--rollback", type=pathlib.Path, default=None, metavar="LOG",
                       help="Reverse insertions using a prior transaction log.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    mind = args.mind or _mind_root()

    if args.dry_run:
        return cmd_dry_run(mind)
    if args.execute:
        return cmd_execute(mind)
    if args.measure:
        return cmd_measure(mind)
    if args.final_measure:
        return cmd_final_measure(mind)
    if args.rollback:
        return cmd_rollback(mind, args.rollback)
    parser.error("no subcommand selected")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
