"""A2UI emitter for the Bugs page (issue #651).

Reuses :func:`status_site.server.collect_bugs` for the bucket data —
the server already owns Open / Waiting on review / In progress / Fixed
/ Won't fix / Dispatch failed, built from each ``bug-<N>.md`` detail
file's frontmatter on every call. This module wraps those buckets as
an A2UI surface (``bugs``) ridden over the existing SSE stream as the
``page:bugs`` section.

Pure formatting helpers that used to live in ``server.py`` —
:func:`format_usd_local` and :func:`format_pricing_segment` — moved
here since they're pure string formatters with no server coupling and
the Bugs page is their only caller shape now. ``server.py`` re-exports
them under their old private names for back-compat with
``test_status_site_bugs_pricing.py`` (which imports them directly).
"""

from __future__ import annotations

from pathlib import Path

from status_site.a2ui import (
    component,
    create_surface,
    update_components,
    update_data_model,
)


SURFACE_ID = "bugs"

# Section order mirrors ``render_bugs_md`` (and the legacy page): Open
# first, then Waiting on review (promoted from In progress on draft-PR
# presence), In progress, Fixed, Won't fix, Dispatch failed last with
# a warning prefix so Jason spots triage items.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    # (bucket_key, heading, warning_prefix)
    ("open", "Open", ""),
    ("waiting-on-review", "Waiting on review", ""),
    ("in-progress", "In progress", ""),
    ("fixed", "Fixed", ""),
    ("wontfix", "Won't fix", ""),
    ("dispatch-failed", "Dispatch failed (needs triage)", "⚠ "),
)


# ─── pricing formatters (moved from server.py) ───────────────────────


def format_usd_local(dollars: float) -> str:
    """Status-site-local USD formatter.

    Mirrors ``alice_pricing.format_usd`` without taking a dep on the
    pricing package — ``status_site`` is stdlib-only by design. Keep
    in sync.
    """
    if dollars == 0:
        return "$0.00"
    if dollars < 0:
        return "-" + format_usd_local(-dollars)
    if dollars < 0.01:
        return f"${dollars:.4g}"
    return f"${dollars:.2f}"


def format_pricing_segment(estimate_usd: str, actual_usd: str) -> str:
    """Build the ``$X est / $Y actual`` tail for a bug's summary.

    * both present  → ``$X est / $Y actual``
    * only estimate → ``$X est``
    * only actual   → ``$Y actual``
    * neither       → ``""`` (caller omits the segment entirely)

    Values that don't parse as floats, or that are the sentinel
    strings ``null`` / ``none``, are treated as absent — a malformed
    frontmatter row shouldn't crash the render.
    """

    def _render(raw: str) -> str:
        s = (raw or "").strip()
        if not s or s.lower() in ("null", "none"):
            return ""
        try:
            dollars = float(s)
        except ValueError:
            return ""
        return format_usd_local(dollars)

    est = _render(estimate_usd)
    act = _render(actual_usd)
    if est and act:
        return f"{est} est / {act} actual"
    if est:
        return f"{est} est"
    if act:
        return f"{act} actual"
    return ""


# ─── A2UI builders ───────────────────────────────────────────────────


def _bug_row(bucket_key: str, prefix: str, bug: dict) -> dict:
    """One bug's typed row for the data model.

    Includes the fields the client's list renderer needs to build each
    bullet: id (for React keys), title, summary bits, and the pricing
    pill text — keeps the client dumb.
    """
    summary_bits: list[str] = []
    if bug.get("summary"):
        summary_bits.append(bug["summary"])
    pr_url = bug.get("pr_url") or ""
    if bucket_key in ("in-progress", "waiting-on-review") and pr_url:
        summary_bits.append(f"[draft PR]({pr_url})")
    pricing_segment = format_pricing_segment(
        bug.get("estimate_usd", ""), bug.get("actual_usd", "")
    )
    return {
        "id": f"bug-{bug['report_id']}",
        "report_id": bug["report_id"],
        "title": f"{prefix}bug-{bug['report_id']} — {bug['title']}",
        "summary_bits": summary_bits,
        "pr_url": pr_url,
        "pricing": pricing_segment,
        "depends_on": bug.get("depends_on") or [],
    }


def sum_pricing(buckets: dict[str, list[dict]]) -> dict[str, float]:
    """``{"estimate": total, "actual": total}`` across every bug.

    Shared with ``render_bugs_md``'s Pipeline P&L footer; splits off
    the sum from the markdown-stringification so the A2UI emitter can
    use the same source of truth.
    """
    totals = {"estimate": 0.0, "actual": 0.0}
    for items in buckets.values():
        for b in items:
            for key, bucket in (
                ("estimate_usd", "estimate"),
                ("actual_usd", "actual"),
            ):
                raw = (b.get(key) or "").strip()
                if not raw or raw.lower() in ("null", "none"):
                    continue
                try:
                    totals[bucket] += float(raw)
                except ValueError:
                    continue
    return totals


def build_envelopes(status_dir: Path) -> list[dict]:
    """Return the full A2UI envelope list for the Bugs surface.

    Uses :func:`status_site.server.collect_bugs` for bucket data so
    the Bugs page and the ``/bugs.md`` plaintext route can't drift —
    they read the same source. Data model shape::

        {
          "sections": [
            {"key": "open", "heading": "Open", "rows": [bug_row, ...]},
            ...
          ],
          "pipeline": {
            "estimate": "$1.23",
            "actual": "$0.45",
            "delta": "-$0.78",
          } | null,
        }

    ``pipeline`` is ``None`` when nothing has been priced so the Bugs
    page reads clean before the first dispatched tick (same behavior
    as the markdown renderer).
    """
    # Local import to avoid the server↔sources cycle at module load.
    from status_site.server import collect_bugs

    buckets, _ = collect_bugs(status_dir)
    section_rows = []
    for key, heading, prefix in SECTIONS:
        rows = [_bug_row(key, prefix, b) for b in buckets.get(key, [])]
        section_rows.append({"key": key, "heading": heading, "rows": rows})

    totals = sum_pricing(buckets)
    if totals["estimate"] > 0 or totals["actual"] > 0:
        delta = totals["actual"] - totals["estimate"]
        pipeline: dict | None = {
            "estimate": format_usd_local(totals["estimate"]),
            "actual": format_usd_local(totals["actual"]),
            "delta": format_usd_local(delta),
        }
    else:
        pipeline = None

    return [
        create_surface(
            SURFACE_ID,
            data={"sections": [], "pipeline": None},
        ),
        update_components(
            SURFACE_ID,
            [
                component(
                    "root",
                    "list",
                    props={"variant": "bugs-page"},
                    children=["header", "sections", "pipeline"],
                ),
                component(
                    "header",
                    "text",
                    props={
                        "markdown": (
                            "# In-game bug reports\n\n"
                            "Every /bug report that reaches me from the "
                            "game, grouped by status."
                        ),
                    },
                ),
                component(
                    "sections",
                    "list",
                    props={"boundTo": "/sections", "variant": "bugs-sections"},
                ),
                component(
                    "pipeline",
                    "pill",
                    props={"boundTo": "/pipeline", "variant": "pipeline-pl"},
                ),
            ],
        ),
        update_data_model(SURFACE_ID, "/sections", section_rows),
        update_data_model(SURFACE_ID, "/pipeline", pipeline),
    ]


def render(status_dir: Path) -> dict:
    """Section renderer for ``page:bugs``. Emits ``{"a2ui": [...]}``."""
    return {"a2ui": build_envelopes(status_dir)}
