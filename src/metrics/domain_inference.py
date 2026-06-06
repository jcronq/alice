"""Domain inference from tags — automated domain assignment for unknown-domain notes.

Reads all notes without a ``domain:`` frontmatter field, checks if any of their
tags matches a known domain value, and produces a mapping report.  Handles exact
tag→domain matches and a small set of known partial matches (e.g. ``cue-runner``
→ ``cue-runner-evaluation``).

Designed as a low-risk automation candidate: the module is read-only by default
(produces a report).  Run with ``--apply`` to actually add domain fields to
notes.  See [[domain-inference-from-tags]] for the research basis (76.8%
recoverable in dry-run).

Usage:
    python3 -m metrics.domain_inference --vault /path/to/vault
    python3 -m metrics.domain_inference --vault /path/to/vault --apply --dry-run
    python3 -m metrics.domain_inference --vault /path/to/vault --apply
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from indexer.yaml_lite import split_frontmatter

logger = logging.getLogger(__name__)

# Known partial-match rules: tag value → domain value.
# Currently only one: "cue-runner" tag maps to "cue-runner-evaluation" domain.
PARTIAL_MATCH_RULES: dict[str, str] = {
    "cue-runner": "cue-runner-evaluation",
}

# Tags that are too generic to be used for domain inference.
# These appear frequently in non-domain contexts and would produce false positives.
EXCLUDED_TAGS: frozenset[str] = frozenset({
    "research", "design", "reference", "implementation", "bug",
    "stage-d", "stage-c", "stage-b", "stage-a",
    "project", "hub", "active", "complete", "dispatched",
    "automation", "convention", "spec", "protocol",
})


def _read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Read a markdown file and return its frontmatter dict, or None."""
    try:
        text = path.read_text(encoding="utf-8")
        fm, _ = split_frontmatter(text)
        return fm if fm else None
    except Exception:
        return None


def _write_frontmatter(path: Path, fm: dict[str, Any]) -> None:
    """Rewrite a markdown file with updated frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
        _, body = split_frontmatter(text)
        # Re-serialize frontmatter
        lines = ["---"]
        for key, value in fm.items():
            if isinstance(value, list):
                lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
            elif isinstance(value, str):
                lines.append(f"{key}: {value}")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            else:
                lines.append(f"{key}: {value}")
        lines.append("---")
        new_text = "\n".join(lines) + "\n" + body.lstrip("\n")
        path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to write {path}: {e}")
        raise


def collect_known_domains(vault: Path) -> set[str]:
    """Collect all unique domain values from notes that have a domain: field."""
    domains: set[str] = set()
    for md_file in vault.rglob("*.md"):
        if md_file.name in ("index.md", "README.md", "unresolved.md"):
            continue
        if "/dailies/" in str(md_file):
            continue
        fm = _read_frontmatter(md_file)
        if fm and "domain" in fm:
            domains.add(fm["domain"])
    return domains


def collect_tag_domain_pairs(vault: Path) -> dict[str, set[str]]:
    """Build a reverse index: tag → set of domains it appears with.

    Only includes notes that have both tags and a domain field.
    Returns {tag: {domain1, domain2, ...}}.
    """
    tag_to_domains: dict[str, set[str]] = defaultdict(set)
    for md_file in vault.rglob("*.md"):
        if md_file.name in ("index.md", "README.md", "unresolved.md"):
            continue
        if "/dailies/" in str(md_file):
            continue
        fm = _read_frontmatter(md_file)
        if not fm:
            continue
        domain = fm.get("domain")
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if domain and tags:
            for tag in tags:
                if tag and not tag.startswith("[["):
                    tag_to_domains[tag].add(domain)
    return tag_to_domains


def infer_domain(tag: str, known_domains: set[str]) -> str | None:
    """Check if a tag maps to a known domain.

    Returns the domain name if there's an exact or partial match, None otherwise.
    """
    # Exact match
    if tag in known_domains:
        return tag
    # Partial match
    if tag in PARTIAL_MATCH_RULES:
        mapped = PARTIAL_MATCH_RULES[tag]
        if mapped in known_domains:
            return mapped
    return None


def find_unknown_domain_notes(vault: Path) -> list[dict[str, Any]]:
    """Find all notes without a domain: field.

    Returns list of dicts with: path, tags, slug, title.
    """
    unknown_notes: list[dict[str, Any]] = []
    for md_file in vault.rglob("*.md"):
        if md_file.name in ("index.md", "README.md", "unresolved.md"):
            continue
        if "/dailies/" in str(md_file):
            continue
        fm = _read_frontmatter(md_file)
        if not fm:
            continue
        if "domain" in fm:
            continue
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        unknown_notes.append({
            "path": str(md_file),
            "tags": tags,
            "slug": fm.get("slug", ""),
            "title": fm.get("title", ""),
        })
    return unknown_notes


def analyze_notes(
    vault: Path,
    known_domains: set[str],
    tag_domain_pairs: dict[str, set[str]],
) -> dict[str, Any]:
    """Analyze unknown-domain notes against known domains and tag patterns.

    Returns a report dict with counts, mappings, and per-note details.
    """
    unknown_notes = find_unknown_domain_notes(vault)
    total = len(unknown_notes)

    recovered: list[dict[str, Any]] = []
    not_recovered: list[dict[str, Any]] = []

    for note in unknown_notes:
        # Filter out excluded tags
        candidate_tags = [t for t in note["tags"] if t and t not in EXCLUDED_TAGS]

        inferred_domain = None
        inferred_tag = None

        for tag in candidate_tags:
            domain = infer_domain(tag, known_domains)
            if domain:
                inferred_domain = domain
                inferred_tag = tag
                break

        if inferred_domain:
            recovered.append({
                "path": note["path"],
                "slug": note["slug"],
                "title": note["title"],
                "inferred_domain": inferred_domain,
                "inferred_from_tag": inferred_tag,
                "all_tags": note["tags"],
            })
        else:
            not_recovered.append(note)

    # Tag frequency analysis among recovered notes
    tag_frequency: dict[str, int] = defaultdict(int)
    for entry in recovered:
        tag_frequency[entry["inferred_from_tag"]] += 1

    # Domain distribution
    domain_dist: dict[str, int] = defaultdict(int)
    for entry in recovered:
        domain_dist[entry["inferred_domain"]] += 1

    return {
        "total_unknown": total,
        "recovered_count": len(recovered),
        "not_recovered_count": len(not_recovered),
        "recovery_rate": len(recovered) / total if total > 0 else 0,
        "tag_frequency": dict(sorted(tag_frequency.items(), key=lambda x: -x[1])),
        "domain_distribution": dict(sorted(domain_dist.items(), key=lambda x: -x[1])),
        "recovered_notes": recovered,
        "not_recovered_notes": not_recovered,
    }


def apply_inference(report: dict[str, Any], vault: Path, dry_run: bool = False) -> dict[str, Any]:
    """Apply domain inference to notes that have a recoverable tag match.

    If dry_run is True, only prints what would be changed.
    Returns a summary of changes.
    """
    changes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for entry in report["recovered_notes"]:
        md_path = Path(entry["path"])
        try:
            fm = _read_frontmatter(md_path)
            if not fm:
                errors.append({"path": entry["path"], "error": "no frontmatter"})
                continue

            fm["domain"] = entry["inferred_domain"]
            if "status" not in fm:
                fm["status"] = "inferred"
            else:
                existing_status = fm["status"]
                if existing_status != "inferred":
                    fm["status"] = f"{existing_status}, inferred"

            if not dry_run:
                _write_frontmatter(md_path, fm)

            changes.append({
                "path": entry["path"],
                "slug": entry["slug"],
                "domain": entry["inferred_domain"],
                "from_tag": entry["inferred_from_tag"],
            })
        except Exception as e:
            errors.append({"path": entry["path"], "error": str(e)})

    return {
        "changes_applied": len(changes),
        "changes": changes,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Domain inference from tags — automated domain assignment"
    )
    parser.add_argument(
        "--vault", type=Path, required=True,
        help="Path to the vault (cortex-memory directory)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually add domain fields to notes (default: report only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Write report to file instead of stdout",
    )
    args = parser.parse_args()

    known_domains = collect_known_domains(args.vault)
    tag_domain_pairs = collect_tag_domain_pairs(args.vault)
    report = analyze_notes(args.vault, known_domains, tag_domain_pairs)

    if args.apply:
        result = apply_inference(report, args.vault, dry_run=args.dry_run)
        report["apply_result"] = result

    output_text = json.dumps(report, indent=2) if args.json else format_report(report)
    if args.output:
        args.output.write_text(output_text)
        print(f"Report written to {args.output}")
    else:
        print(output_text)


def format_report(report: dict[str, Any]) -> str:
    """Format the analysis report as human-readable text."""
    lines = []
    lines.append("Domain Inference Report")
    lines.append("=" * 40)
    lines.append("")
    lines.append(f"Total unknown-domain notes: {report['total_unknown']}")
    lines.append(f"Recoverable via tags: {report['recovered_count']}")
    lines.append(f"Not recoverable: {report['not_recovered_count']}")
    rate = report['recovery_rate']
    lines.append(f"Recovery rate: {rate:.1%}")
    lines.append("")

    if report["tag_frequency"]:
        lines.append("Top tags driving inference:")
        for tag, count in list(report["tag_frequency"].items())[:15]:
            lines.append(f"  {tag}: {count}")
        lines.append("")

    if report["domain_distribution"]:
        lines.append("Domain distribution (recovered notes):")
        for domain, count in list(report["domain_distribution"].items())[:15]:
            lines.append(f"  {domain}: {count}")
        lines.append("")

    if report.get("apply_result"):
        result = report["apply_result"]
        lines.append("Apply result:")
        lines.append(f"  Changes: {result['changes_applied']}")
        lines.append(f"  Errors: {len(result['errors'])}")
        if result["errors"]:
            lines.append("  Error details:")
            for err in result["errors"][:5]:
                lines.append(f"    {err['path']}: {err['error']}")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
