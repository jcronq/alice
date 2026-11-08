"""Resources page — what every background worker is burning.

Two entry points:

* :func:`render_resources_md` — legacy markdown body, still used by
  ``/resources.md`` for the plaintext route.
* :func:`render` (``page:resources`` A2UI section) — wraps the same
  markdown in a surface envelope so the reactive shell's generic
  ``A2uiSurface`` component mounts it when the hash route lands on
  ``#resources`` (otherwise the nav tab silently falls through to
  Overview — see parseHash in ``static/index.html``).

Three sections, rendered live from ``alice-state.db`` on each request:

1. **Snapshot** — top 20 processes by current CPU% (and separately by
   RSS). Rows carry ``pid``, ``cmd_tag``, correlated ``bg_id`` (if
   known), ``cpu_pct``, ``rss_mb``, ``age``.
2. **Timeline** — stacked 2-hour CPU chart, bucketed at 5-minute
   intervals, colored per bg_id. Inline SVG. Below the chart: a
   legend mapping each bg_id slice to a short label.
3. **Tokens** — last 24 h of ``token_usage`` summed per ``(bg_id,
   model)``: input, output, cache-creation, cache-read.

Layout stays under 500 KB uncompressed — the SVG is a stacked-area
polygon per bg_id, never per-sample. The renderer is pure except for
``read_api.connect`` and ``datetime.now``; both inject for tests.

Written for issue #860.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from status_site.a2ui import (
    component,
    create_surface,
    update_components,
)
from status_site.sources.common import staleness_footer

log = logging.getLogger("status_site.resources")

SOURCE_NAME = "alice-state-db"

#: Timeline window.
TIMELINE_HOURS = 2
#: Bucket width for the stacked chart.
BUCKET_MINUTES = 5
#: Snapshot table caps.
SNAPSHOT_LIMIT = 20
#: Token-usage window.
TOKEN_WINDOW_HOURS = 24

#: Eight-hue palette for the stacked chart. CSS color names so the SVG
#: stays human-readable; browsers render them identically. Picked so
#: adjacent hues are distinguishable in light + dark mode.
LEGEND_PALETTE: Sequence[str] = (
    "#3b82f6",  # blue
    "#f97316",  # orange
    "#10b981",  # green
    "#a855f7",  # purple
    "#eab308",  # yellow
    "#ef4444",  # red
    "#06b6d4",  # cyan
    "#ec4899",  # pink
)
#: Everything beyond the palette collapses into this slate color.
OVERFLOW_COLOR = "#64748b"


# ─── row shapes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SnapshotRow:
    pid: int
    cmd_tag: str
    bg_id: Optional[str]
    cpu_pct: float
    rss_mb: float
    ts: str


@dataclass(frozen=True)
class TimelineBucket:
    """One (bucket_start, series_key, cpu_pct) triple.

    Series key is the bg_id when present, falls back to cmd_tag so
    long-lived services (speaking, thinking, …) each get their own
    stack slice.
    """

    bucket_start: datetime
    series: str  # bg_id or ``"<cmd_tag:tag>"`` sentinel
    cpu_pct: float


@dataclass(frozen=True)
class TokenTotal:
    bg_id: Optional[str]
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int


# ─── DB access (thin wrappers — injectable) ────────────────────────


def _connect(db_path: Optional[Path] = None):
    """Return the :mod:`alice_state_db.read_api` context manager.

    Exposed as a function so tests can monkeypatch the whole import.
    Production callers use it with ``with _connect() as conn:``; tests
    can return a lightweight CM wrapping a hand-built sqlite3 handle.

    When ``db_path`` is None, honor ``ALICE_STATE_DB_PATH`` (the same
    env var ``db_readers._resolve_db_path`` checks) before falling
    through to ``DEFAULT_DB_PATH``. Without this the status-site
    container reads from ``/root/alice-mind/inner/state/alice-state.db``
    (an empty fallback) instead of ``/srv/state/alice-state.db``.
    """
    from alice_state_db.read_api import connect

    if db_path is None:
        import os
        env = os.environ.get("ALICE_STATE_DB_PATH")
        if env:
            db_path = Path(env)
    return connect(db_path=db_path)


def fetch_snapshot(
    conn, *, limit: int = SNAPSHOT_LIMIT, order_by: str = "cpu_pct",
) -> list[SnapshotRow]:
    """Latest sample for each (pid, cmd_tag), ordered by cpu_pct or rss_mb.

    ``resource_samples`` has one row per (pid, sample tick). This
    returns the *most recent* row per pid in the last 2 min, so the
    snapshot doesn't show zombies that exited 10 min ago.
    """
    assert order_by in ("cpu_pct", "rss_mb"), order_by
    # ``ts`` is stored as ``YYYY-MM-DDTHH:MM:SS+00:00`` (python's
    # datetime.isoformat with TZ). SQLite's ``datetime('now', ...)``
    # returns ``YYYY-MM-DD HH:MM:SS`` (no 'T', no TZ), so a direct
    # string compare mis-orders because 'T' > ' '. We normalise the
    # cutoff via ``strftime`` to match the stored format exactly.
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT pid, MAX(ts) AS ts
            FROM resource_samples
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-2 minutes')
            GROUP BY pid
        )
        SELECT r.pid, r.cmd_tag, r.bg_id, r.cpu_pct, r.rss_mb, r.ts
        FROM resource_samples r
        JOIN latest l ON l.pid = r.pid AND l.ts = r.ts
        ORDER BY r.{order_by} DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[SnapshotRow] = []
    for r in rows:
        out.append(
            SnapshotRow(
                pid=int(r[0]),
                cmd_tag=str(r[1] or ""),
                bg_id=(str(r[2]) if r[2] else None),
                cpu_pct=float(r[3] or 0.0),
                rss_mb=float(r[4] or 0.0),
                ts=str(r[5] or ""),
            )
        )
    return out


def fetch_timeline(
    conn, *, hours: int = TIMELINE_HOURS, bucket_minutes: int = BUCKET_MINUTES,
) -> list[TimelineBucket]:
    """Average CPU per (bucket, series) over the last ``hours`` hours.

    Series = bg_id when present, otherwise ``cmd_tag:<tag>`` so long-
    lived services stay visible. The bucket boundaries are aligned to
    ``bucket_minutes`` so all series share the x-axis.
    """
    # SQLite doesn't have a "bucket to N minutes" native helper, but
    # strftime('%s', ts) / (N*60) rounds correctly for ISO-with-TZ
    # timestamps. We re-materialise the bucket's ISO start on the
    # Python side for the chart renderer.
    rows = conn.execute(
        """
        SELECT
            CAST(strftime('%s', ts) AS INTEGER) / (? * 60) AS bucket_slot,
            COALESCE(bg_id, 'cmd_tag:' || cmd_tag) AS series,
            AVG(cpu_pct) AS avg_cpu
        FROM resource_samples
        WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?)
        GROUP BY bucket_slot, series
        ORDER BY bucket_slot ASC
        """,
        (bucket_minutes, f"-{hours} hours"),
    ).fetchall()
    out: list[TimelineBucket] = []
    for r in rows:
        bucket_slot = int(r[0])
        series = str(r[1] or "")
        cpu = float(r[2] or 0.0)
        bucket_start = datetime.fromtimestamp(
            bucket_slot * bucket_minutes * 60, tz=timezone.utc
        )
        out.append(
            TimelineBucket(bucket_start=bucket_start, series=series, cpu_pct=cpu)
        )
    return out


def fetch_token_totals(
    conn, *, hours: int = TOKEN_WINDOW_HOURS,
) -> list[TokenTotal]:
    """Sum of per-turn token counts grouped by (bg_id, model)."""
    rows = conn.execute(
        """
        SELECT bg_id, model,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_creation_tokens), SUM(cache_read_tokens)
        FROM token_usage
        WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?)
        GROUP BY bg_id, model
        ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """,
        (f"-{hours} hours",),
    ).fetchall()
    out: list[TokenTotal] = []
    for r in rows:
        out.append(
            TokenTotal(
                bg_id=(str(r[0]) if r[0] else None),
                model=(str(r[1]) if r[1] else None),
                input_tokens=int(r[2] or 0),
                output_tokens=int(r[3] or 0),
                cache_creation_tokens=int(r[4] or 0),
                cache_read_tokens=int(r[5] or 0),
            )
        )
    return out


# ─── formatting helpers ────────────────────────────────────────────


def _fmt_int(n: int) -> str:
    """``1234567 → 1,234,567``. Separator keeps 7-digit token counts legible."""
    return f"{int(n):,}"


def _short_bg(bg_id: Optional[str]) -> str:
    """Render ``bg_id`` as a tight label — first 8 chars + ellipsis."""
    if not bg_id:
        return "—"
    if len(bg_id) <= 10:
        return bg_id
    return bg_id[:8] + "…"


def _series_label(series: str) -> str:
    """Short label for a timeline series.

    ``bg_xxx`` → first 8 chars; ``cmd_tag:thinking`` → ``thinking``.
    """
    if series.startswith("cmd_tag:"):
        return series.split(":", 1)[1]
    return _short_bg(series)


# ─── renderers ─────────────────────────────────────────────────────


def render_snapshot_table(rows: Sequence[SnapshotRow], *, title: str) -> str:
    """Return a markdown table (not HTML) for the snapshot section."""
    head = (
        f"\n### {title}\n\n"
        "| PID | Tag | bg_id | CPU % | RSS MB | Sampled |\n"
        "| ---:| --- | --- | ---:| ---:| --- |\n"
    )
    if not rows:
        return head + "| *(no samples yet)* | | | | | |\n"
    body = []
    for r in rows:
        body.append(
            f"| {r.pid} | {r.cmd_tag} | {_short_bg(r.bg_id)} "
            f"| {r.cpu_pct:.1f} | {r.rss_mb:.0f} | {r.ts} |"
        )
    return head + "\n".join(body) + "\n"


def render_timeline_svg(
    buckets: Sequence[TimelineBucket],
    *,
    hours: int = TIMELINE_HOURS,
    bucket_minutes: int = BUCKET_MINUTES,
    width: int = 900,
    height: int = 300,
) -> str:
    """Render the stacked-area CPU chart as inline SVG + a legend list.

    Returns an SVG string followed by a markdown unordered-list legend.
    When ``buckets`` is empty, returns a single placeholder paragraph
    so the section still has visible content.
    """
    if not buckets:
        return (
            "<p><em>No resource samples in the last "
            f"{hours} h — the sampler may still be warming up.</em></p>\n"
        )

    # Series + x-axis slots.
    series_set: list[str] = []
    seen: set[str] = set()
    for b in buckets:
        if b.series not in seen:
            seen.add(b.series)
            series_set.append(b.series)
    # Stable rank by total CPU so dominant series get the top-palette
    # colors — makes the chart readable at a glance.
    totals = {s: 0.0 for s in series_set}
    for b in buckets:
        totals[b.series] += b.cpu_pct
    series_set.sort(key=lambda s: totals[s], reverse=True)
    palette = {
        s: (LEGEND_PALETTE[i] if i < len(LEGEND_PALETTE) else OVERFLOW_COLOR)
        for i, s in enumerate(series_set)
    }

    bucket_slots = sorted({b.bucket_start for b in buckets})
    slot_index = {b: i for i, b in enumerate(bucket_slots)}
    if len(bucket_slots) < 2:
        # One data point can't stack; render a bar instead.
        total = sum(b.cpu_pct for b in buckets)
        return (
            f"<p><em>Only one bucket of data so far — total CPU "
            f"{total:.0f}% across {len(series_set)} series. The chart "
            f"appears once two buckets are populated.</em></p>\n"
        )

    # Build the matrix [slot][series] → cpu.
    matrix: dict[tuple[int, str], float] = {}
    for b in buckets:
        key = (slot_index[b.bucket_start], b.series)
        matrix[key] = matrix.get(key, 0.0) + b.cpu_pct

    # Stack: top of each series = cumulative sum below it.
    n_slots = len(bucket_slots)
    stack_top: list[dict[str, float]] = []
    per_slot_running = [0.0] * n_slots
    for s in series_set:
        row = {}
        for i in range(n_slots):
            v = matrix.get((i, s), 0.0)
            per_slot_running[i] += v
            row[f"slot{i}"] = per_slot_running[i]
        stack_top.append(row)

    y_max = max(per_slot_running) if any(per_slot_running) else 100.0
    # Give the chart ~10% headroom.
    y_axis_max = max(100.0, math.ceil(y_max / 50.0) * 50.0)

    pad_l, pad_r, pad_t, pad_b = 40, 10, 10, 20
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step_x = plot_w / max(n_slots - 1, 1)

    def _xy(slot: int, value: float) -> tuple[float, float]:
        x = pad_l + slot * step_x
        y = pad_t + plot_h - (value / y_axis_max) * plot_h
        return x, y

    svg_parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'aria-label="Stacked CPU by background worker over the last 2 hours">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        'fill="#f8fafc" />',
    ]

    # One polygon per series, drawn bottom-up so stacking works.
    prev_tops = [pad_t + plot_h] * n_slots  # y-pixel of the bottom edge
    for s_idx, s in enumerate(series_set):
        row = stack_top[s_idx]
        # Forward edge (bottom-to-top running sum).
        tops: list[tuple[float, float]] = []
        for i in range(n_slots):
            x, y = _xy(i, row[f"slot{i}"])
            tops.append((x, y))
        # Polygon: forward along tops, then back along prev_tops.
        pts: list[str] = []
        for x, y in tops:
            pts.append(f"{x:.1f},{y:.1f}")
        for i in range(n_slots - 1, -1, -1):
            x = pad_l + i * step_x
            pts.append(f"{x:.1f},{prev_tops[i]:.1f}")
        svg_parts.append(
            f'<polygon points="{" ".join(pts)}" fill="{palette[s]}" '
            'fill-opacity="0.75" stroke="white" stroke-width="0.5" />'
        )
        prev_tops = [y for (_, y) in tops]

    # Y-axis gridlines + labels (0, 50%, 100%, y_axis_max).
    ticks = [0.0, y_axis_max * 0.5, y_axis_max]
    for tv in ticks:
        _, ty = _xy(0, tv)
        svg_parts.append(
            f'<line x1="{pad_l}" y1="{ty:.1f}" x2="{width - pad_r}" '
            f'y2="{ty:.1f}" stroke="#cbd5e1" stroke-width="0.5" '
            'stroke-dasharray="2,2" />'
        )
        svg_parts.append(
            f'<text x="{pad_l - 4}" y="{ty + 3:.1f}" font-size="10" '
            f'text-anchor="end" fill="#475569">{tv:.0f}%</text>'
        )

    # X-axis labels: first, middle, last bucket.
    for idx in {0, n_slots // 2, n_slots - 1}:
        if idx >= n_slots:
            continue
        x = pad_l + idx * step_x
        label = bucket_slots[idx].strftime("%H:%M")
        svg_parts.append(
            f'<text x="{x:.1f}" y="{height - 4}" font-size="10" '
            f'text-anchor="middle" fill="#475569">{label} UTC</text>'
        )

    svg_parts.append("</svg>")
    svg = "\n".join(svg_parts)

    legend_lines = ["", "**Legend**", ""]
    for s in series_set:
        color = palette[s]
        label = _series_label(s)
        legend_lines.append(
            f'- <span style="display:inline-block;width:12px;'
            f'height:12px;background:{color};margin-right:6px;'
            f'vertical-align:middle;"></span> `{label}` '
            f'({totals[s]:.0f}% · cum)'
        )
    return svg + "\n" + "\n".join(legend_lines) + "\n"


def render_token_table(rows: Sequence[TokenTotal]) -> str:
    """Markdown table of per-worker + per-model token spend."""
    head = (
        "\n### Token usage (last 24 h)\n\n"
        "| bg_id | Model | Input | Output | Cache-write | Cache-read |\n"
        "| --- | --- | ---:| ---:| ---:| ---:|\n"
    )
    if not rows:
        return head + "| *(no token samples yet)* | | | | | |\n"
    body = []
    for r in rows:
        body.append(
            f"| {_short_bg(r.bg_id)} | {r.model or '—'} "
            f"| {_fmt_int(r.input_tokens)} | {_fmt_int(r.output_tokens)} "
            f"| {_fmt_int(r.cache_creation_tokens)} "
            f"| {_fmt_int(r.cache_read_tokens)} |"
        )
    return head + "\n".join(body) + "\n"


def render_resources_md(
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> str:
    """Build the full ``/resources.md`` body.

    Opens a read-only connection, renders the three sections, and
    appends the common staleness footer. On DB error the page falls
    back to a single-paragraph diagnostic so Jason can see which
    half's broken.
    """
    try:
        cm = _connect(db_path)
    except Exception as exc:  # noqa: BLE001
        return (
            f"# Resources\n\n"
            f"*(cannot open state DB: `{exc}`)*\n\n"
            f"{staleness_footer(SOURCE_NAME, now=now)}\n"
        )
    try:
        with cm as conn:
            snap_cpu = fetch_snapshot(conn, order_by="cpu_pct")
            snap_rss = fetch_snapshot(conn, order_by="rss_mb")
            timeline = fetch_timeline(conn)
            tokens = fetch_token_totals(conn)
    except Exception as exc:  # noqa: BLE001
        return (
            f"# Resources\n\n"
            f"*(query against state DB failed: `{exc}`)*\n\n"
            f"{staleness_footer(SOURCE_NAME, now=now)}\n"
        )

    header = (
        "# Resources\n\n"
        "What each background worker is consuming right now, over the "
        "last 2 hours, and in the last 24 hours of Claude calls. "
        "Writes land every 30 s (CPU/RSS) and 60 s (tokens).\n"
    )
    out = [
        header,
        "## Snapshot\n",
        render_snapshot_table(snap_cpu, title="Top by CPU"),
        render_snapshot_table(snap_rss, title="Top by RSS"),
        f"\n## Timeline (last {TIMELINE_HOURS} h, {BUCKET_MINUTES}-min buckets, CPU %)\n\n",
        render_timeline_svg(timeline),
        "\n## Tokens\n",
        render_token_table(tokens),
        "\n",
        staleness_footer(SOURCE_NAME, now=now),
        "\n",
    ]
    return "".join(out)


# ─── A2UI surface (page:resources, issue #861 fix) ──────────────────

SURFACE_ID = "resources"


def build_envelopes(
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Wrap :func:`render_resources_md` in an A2UI surface envelope list.

    The shell's generic :class:`A2uiSurface` renderer mounts whichever
    ``page:<slug>`` payload matches the current hash route; without
    this envelope list the ``#resources`` tab has nothing to mount
    and the route silently falls through to Overview.

    Shape mirrors ``sources/bugs.build_envelopes``: a ``createSurface``
    opener followed by an ``updateComponents`` with a ``root`` list
    whose single child is a ``text`` component carrying the markdown
    body in ``props.markdown`` — same prop the chat/bugs surfaces
    already use for prose blocks.
    """
    md = render_resources_md(db_path=db_path, now=now)
    return [
        create_surface(SURFACE_ID, data={}),
        update_components(
            SURFACE_ID,
            [
                component(
                    "root",
                    "list",
                    props={"variant": "resources-page"},
                    children=["body"],
                ),
                component(
                    "body",
                    "text",
                    props={"markdown": md},
                ),
            ],
        ),
    ]


def render(cfg=None) -> dict:  # noqa: ARG001 — cfg accepted for section-renderer uniformity
    """Section renderer for ``page:resources``. Emits ``{"a2ui": [...]}``.

    ``cfg`` is accepted (and ignored) so the server-side section
    registration can call this with the same ``cfg``-threading pattern
    the other ``page:<slug>`` renderers use — there's nothing in
    :class:`status_site.server.Config` the resources renderer needs
    beyond the default state-DB path, which :func:`_connect` resolves.
    """
    return {"a2ui": build_envelopes()}
