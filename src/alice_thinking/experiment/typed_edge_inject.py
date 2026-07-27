"""Manual typed-edge injection executor (Guard 3, Phase 2 of the eval-first split).

Executes the two operations described in the injection plan JSON:

1. Insert ``link_source='manual'`` rows into ``typed_edges`` (idempotent via the
   ``UNIQUE(from_slug, to_slug, link_type, link_source)`` constraint).
2. Optionally append ``[[to_slug]]`` entries to the source note's
   ``references:`` frontmatter list. Idempotent; skips missing notes with a
   stderr warning.

After the write phase completes, mandatory verification queries are executed
and a verification-results JSON is emitted to stdout per the schema at
``~/alice-mind/cortex-memory/reference/verification-results-schema.md``.

Design: ``~/alice-mind/cortex-memory/research/2026-07-27-typed-edge-injection-script-design.md``
Guard 3 protocol: ``injection-experiment-operating-protocol``
Failure history (why this exists): ``typed-edge-hub-note-injection-failure``,
``injection-fabrication-pattern``.

Phase 1 ships this executable and its tests. Phase 2 will invoke it against
the production DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_TARGET_FIELDS = ("from_slug", "to_slug", "link_type", "context", "confidence")
DEFAULT_DB = "~/alice-mind/inner/state/cortex-index.db"
DEFAULT_PLAN = "~/alice-mind/inner/state/hub-typed-edge-injection-plan.json"
DEFAULT_VAULT = "~/alice-mind/cortex-memory"
DEFAULT_EXPERIMENT_SLUG = "typed-edge-hub-note-experiment"

# Columns the executor requires on typed_edges. Guards against schema drift
# (the failure pattern that made 2026-07-24 injection fail silently).
REQUIRED_TYPED_EDGE_COLUMNS = frozenset(
    {"from_slug", "to_slug", "link_type", "link_source", "context", "confidence"}
)


def _expand(p: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and resolve to an absolute path."""
    return Path(os.path.expanduser(os.fspath(p))).resolve()


def load_plan(path: Path) -> list[dict[str, Any]]:
    """Load and validate the injection plan JSON.

    Raises ``ValueError`` with a caller-actionable message if the shape is
    wrong. This is the "fail loud" gate before any DB touch.
    """
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(
            f"plan {path}: top-level must be a JSON object, got {type(data).__name__}"
        )
    if "targets" not in data:
        raise ValueError(f"plan {path}: missing top-level 'targets' key")
    targets = data["targets"]
    if not isinstance(targets, list):
        raise ValueError(
            f"plan {path}: 'targets' must be a list, got {type(targets).__name__}"
        )
    for i, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError(f"plan {path}: target[{i}] is not an object")
        missing = [f for f in REQUIRED_TARGET_FIELDS if f not in target]
        if missing:
            raise ValueError(
                f"plan {path}: target[{i}] missing required fields: {missing}"
            )
    return targets


def verify_typed_edges_table(conn: sqlite3.Connection) -> None:
    """Ensure ``typed_edges`` exists with the expected columns. Fails loud.

    This catches two failure modes at once: the table doesn't exist (indexer
    hasn't run) and the schema has drifted (column rename would silently write
    to the wrong place).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='typed_edges'"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "typed_edges table not found in DB — run the indexer "
            "(src/indexer/build_index.py) before invoking this script."
        )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(typed_edges)")}
    missing = REQUIRED_TYPED_EDGE_COLUMNS - cols
    if missing:
        raise RuntimeError(
            f"typed_edges schema is missing required columns: {sorted(missing)}"
        )


def insert_edges(
    conn: sqlite3.Connection,
    targets: list[dict[str, Any]],
    link_type_override: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Insert manual edges via ``INSERT OR IGNORE``. Idempotent.

    Returns ``(inserted, skipped_duplicate)``.
    """
    inserted = 0
    skipped = 0
    for target in targets:
        link_type = link_type_override or target["link_type"]
        params = (
            target["from_slug"],
            target["to_slug"],
            link_type,
            "manual",  # HARDCODED — distinguishes from predicate-extracted edges.
            target["context"],
            target["confidence"],
        )
        if dry_run:
            print(f"[dry-run] would INSERT {params}", file=sys.stderr)
            inserted += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO typed_edges "
            "(from_slug, to_slug, link_type, link_source, context, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            params,
        )
        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


def find_note_path(
    conn: sqlite3.Connection, slug: str, vault: Path
) -> Path | None:
    """Resolve a note's file path via the ``notes`` table. ``None`` if missing."""
    row = conn.execute("SELECT path FROM notes WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    rel = row[0]
    candidate = Path(rel) if Path(rel).is_absolute() else (vault / rel)
    return candidate if candidate.exists() else None


def _append_references(text: str, new_slugs: list[str]) -> tuple[str, bool]:
    """Append ``[[slug]]`` entries to a note's ``references:`` frontmatter list.

    Returns ``(updated_text, changed)``. Idempotent — existing wikilinks
    (with or without a trailing description after ``—``) are preserved.
    """
    import yaml  # project dep (pyyaml >= 6)

    if not text.startswith("---"):
        # No frontmatter — construct minimal one.
        refs = "\n".join(f"  - [[{s}]]" for s in new_slugs)
        return f"---\nreferences:\n{refs}\n---\n{text}", True

    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        # Malformed frontmatter (no closing fence); leave alone.
        return text, False
    fm_block = text[4:end_idx]  # skip leading "---\n"
    body = text[end_idx + 4 :]  # skip closing "\n---"
    if body.startswith("\n"):
        body = body[1:]

    try:
        fm = yaml.safe_load(fm_block) or {}
    except yaml.YAMLError:
        return text, False
    if not isinstance(fm, dict):
        return text, False

    existing_raw = fm.get("references", [])
    if not isinstance(existing_raw, list):
        existing_raw = [existing_raw]

    def _slug_of(entry: Any) -> str | None:
        """Extract the slug from a references-list entry.

        Handles three shapes we've seen in the vault:
        - String ``"[[slug]]"`` (well-formed wikilink).
        - String ``"[[slug]] — description"`` (annotated wikilink).
        - ``[['slug']]`` (what PyYAML produces when the frontmatter contains
          the bare ``[[slug]]`` flow syntax — parsed as a nested list).
        """
        if isinstance(entry, list):
            # PyYAML parses `- [[slug]]` as [['slug']] because [[...]] is
            # flow-list syntax. Recover the slug from the single leaf.
            flat = entry
            while isinstance(flat, list) and len(flat) == 1:
                flat = flat[0]
            if isinstance(flat, str):
                return flat.strip()
            return None
        if isinstance(entry, str):
            stripped = entry.strip()
            if stripped.startswith("[[") and "]]" in stripped:
                inner = stripped[2 : stripped.index("]]")]
                return inner.split("|")[0].strip()
        return None

    # Normalize every existing entry to its `[[slug]]` string form so the
    # round-trip through yaml.safe_dump produces stable, quoted output.
    normalized: list[str] = []
    existing_slugs: set[str] = set()
    for entry in existing_raw:
        slug = _slug_of(entry)
        if slug is None:
            # Preserve unrecognised entries verbatim (e.g. free-form strings).
            if isinstance(entry, str):
                normalized.append(entry)
            continue
        if slug in existing_slugs:
            continue  # dedupe existing duplicates while we're here
        existing_slugs.add(slug)
        normalized.append(f"[[{slug}]]")

    changed = False
    for slug in new_slugs:
        if slug not in existing_slugs:
            normalized.append(f"[[{slug}]]")
            existing_slugs.add(slug)
            changed = True
    if not changed:
        return text, False

    fm["references"] = normalized
    new_fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{new_fm_yaml}\n---\n{body}", True


def update_frontmatter_references(
    conn: sqlite3.Connection,
    targets: list[dict[str, Any]],
    vault: Path,
    dry_run: bool,
) -> tuple[int, int]:
    """Group targets by ``from_slug`` and merge new references into each hub note.

    Returns ``(notes_updated, notes_missing)``. Notes whose slug isn't in the
    ``notes`` table are skipped with a stderr warning per the design.
    """
    grouped: dict[str, list[str]] = {}
    for target in targets:
        grouped.setdefault(target["from_slug"], []).append(target["to_slug"])

    updated = 0
    missing = 0
    for from_slug, to_slugs in grouped.items():
        note_path = find_note_path(conn, from_slug, vault)
        if note_path is None:
            print(f"[warn] note not found for slug={from_slug}", file=sys.stderr)
            missing += 1
            continue
        text = note_path.read_text()
        new_text, changed = _append_references(text, to_slugs)
        if not changed:
            continue
        if dry_run:
            print(f"[dry-run] would update frontmatter of {note_path}", file=sys.stderr)
            updated += 1
            continue
        note_path.write_text(new_text)
        updated += 1
    return updated, missing


def run_verification(
    conn: sqlite3.Connection, expected_total_manual: int
) -> tuple[bool, dict[str, Any]]:
    """Run the mandatory post-injection verification queries.

    Returns ``(passed, verification_block)``. The verification block matches
    the shape defined in ``verification-results-schema.md``.
    """
    manual_count = conn.execute(
        "SELECT COUNT(*) FROM typed_edges WHERE link_source = 'manual'"
    ).fetchone()[0]

    by_source_rows = list(
        conn.execute(
            "SELECT link_source, link_type, COUNT(*) FROM typed_edges "
            "GROUP BY link_source, link_type ORDER BY 3 DESC"
        )
    )
    by_source_text = "\n".join(
        "|".join(str(c) for c in row) for row in by_source_rows
    )

    by_target_rows = list(
        conn.execute(
            "SELECT to_slug, COUNT(*) FROM typed_edges "
            "WHERE link_source = 'manual' "
            "GROUP BY to_slug ORDER BY 2 DESC"
        )
    )
    by_target_text = "\n".join(
        "|".join(str(c) for c in row) for row in by_target_rows
    )

    schema_rows = list(conn.execute("PRAGMA table_info(typed_edges)"))
    schema_text = "\n".join("|".join(str(c) for c in row) for row in schema_rows)
    schema_cols = {row[1] for row in schema_rows}
    schema_valid = REQUIRED_TYPED_EDGE_COLUMNS <= schema_cols

    spot_check_rows = list(
        conn.execute(
            "SELECT from_slug, to_slug, link_type, link_source, confidence "
            "FROM typed_edges WHERE link_source = 'manual' LIMIT 10"
        )
    )
    spot_check_text = "\n".join(
        "|".join(str(c) for c in row) for row in spot_check_rows
    )

    passed = manual_count == expected_total_manual and schema_valid

    notes_modified = len({row[0] for row in by_target_rows})

    block: dict[str, Any] = {
        "verification_passed": passed,
        "raw_verification_output": {
            "edge_count_by_source": by_source_text,
            "edge_count_by_target": by_target_text,
            "frontmatter_check": "N/A (frontmatter verification handled separately)",
            "schema_check": schema_text,
            "manual_edge_total": str(manual_count),
            "spot_check": spot_check_text,
        },
        "parsed_counts": {
            "edges_written": manual_count,
            "notes_modified": notes_modified,
            "expected_edges": expected_total_manual,
            "expected_notes": 0,  # caller fills in based on plan
            "schema_valid": schema_valid,
        },
    }
    if not passed:
        if manual_count != expected_total_manual:
            block["failure_reason"] = (
                f"expected {expected_total_manual} manual edges in typed_edges, "
                f"found {manual_count}"
            )
        else:
            block["failure_reason"] = (
                "typed_edges schema missing required columns "
                f"(need {sorted(REQUIRED_TYPED_EDGE_COLUMNS)}, "
                f"got {sorted(schema_cols)})"
            )
    return passed, block


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice_thinking.experiment.typed_edge_inject",
        description=(
            "Inject manual typed edges from a plan JSON. Guard 3 executor — "
            "run only under Speaking's supervision."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"path to cortex-index.db (default: {DEFAULT_DB})")
    parser.add_argument(
        "--targets",
        default=DEFAULT_PLAN,
        help=f"path to injection plan JSON (default: {DEFAULT_PLAN})",
    )
    parser.add_argument(
        "--vault",
        default=DEFAULT_VAULT,
        help=f"path to cortex-memory vault (default: {DEFAULT_VAULT})",
    )
    parser.add_argument(
        "--link-type",
        default=None,
        help="override the per-target link_type field",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned actions; do NOT write to DB or vault",
    )
    parser.add_argument(
        "--skip-frontmatter",
        action="store_true",
        help="only write DB edges; do not touch any note frontmatter",
    )
    parser.add_argument(
        "--experiment-slug",
        default=DEFAULT_EXPERIMENT_SLUG,
        help="slug embedded in the verification-results JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    db_path = _expand(args.db)
    plan_path = _expand(args.targets)
    vault_path = _expand(args.vault)

    targets = load_plan(plan_path)
    expected_notes = len({t["from_slug"] for t in targets})

    conn = sqlite3.connect(db_path)
    try:
        verify_typed_edges_table(conn)

        baseline_manual = conn.execute(
            "SELECT COUNT(*) FROM typed_edges WHERE link_source = 'manual'"
        ).fetchone()[0]

        inserted, skipped_dup = insert_edges(
            conn, targets, args.link_type, args.dry_run
        )

        notes_updated = 0
        notes_missing = 0
        if not args.skip_frontmatter:
            notes_updated, notes_missing = update_frontmatter_references(
                conn, targets, vault_path, args.dry_run
            )

        if args.dry_run:
            conn.rollback()
            summary = {
                "dry_run": True,
                "db_path": str(db_path),
                "plan_path": str(plan_path),
                "planned_inserts": inserted,
                "planned_note_updates": notes_updated,
                "targets_processed": len(targets),
                "unique_source_notes": expected_notes,
            }
            print(json.dumps(summary, indent=2))
            return 0

        conn.commit()

        expected_total = baseline_manual + inserted
        passed, verification = run_verification(conn, expected_total)
        verification["parsed_counts"]["expected_notes"] = expected_notes
        verification["parsed_counts"]["notes_updated_now"] = notes_updated
        verification["parsed_counts"]["notes_missing"] = notes_missing
        verification["parsed_counts"]["inserted_now"] = inserted
        verification["parsed_counts"]["skipped_duplicates"] = skipped_dup
        verification["parsed_counts"]["baseline_manual"] = baseline_manual

        results: dict[str, Any] = {
            "experiment_slug": args.experiment_slug,
            "verification_timestamp": datetime.now(timezone.utc).isoformat(),
            "max_retries": 2,
            "retry_count": 0,
            "rollback_performed": False,
            **verification,
        }
        print(json.dumps(results, indent=2))
        return 0 if passed else 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
