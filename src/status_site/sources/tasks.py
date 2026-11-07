"""Tracked tasks — SM v2 task store as a first-class Overview section (issue #780).

Reads ``~/alice-mind/inner/tasks/task-*/task.yaml`` directly. The index at
``inner/tasks/index.jsonl`` is maintained by a separate re-index pass (see
issue #781) and can lag; this source walks the directory to avoid depending
on it.

Filters:

* Terminal status (``done`` / ``rejected`` / ``cancelled`` / ``closed``) → skip.
* GH-backed tasks → skip. If ``source`` or any ``tags`` entry carries a
  ``github-issue:<owner>/<repo>#<num>`` marker, an ``<owner>/<repo>#<num>``
  reference, or a ``github.com/.../issues/<num>`` URL the task is already
  surfaced by the SM pipeline source (``pipeline.py``) and we don't want to
  double-render it.

Each row carries: ``slug`` (task id), ``title`` (truncated), ``actor`` (owner
of the next action), ``artifact_type`` (tag chip), ``state`` (raw status),
``age_seconds`` (since ``created``, falling back to ``updated`` and then file
mtime). Rows sort oldest-first so stale entries float to the top. A cap
keeps the Overview section readable; overflow collapses to a "+N more" line.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from status_site.sources.common import staleness_footer

SOURCE_NAME = "sm-tasks"

#: Status values we treat as resolved. Anything else (including unknown /
#: missing) is considered in-flight and surfaced.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"done", "rejected", "cancelled", "closed"}
)

#: Cap on rendered rows. Overflow collapses into a "+N more" line so the
#: section stays scannable when the queue is deep. Env override
#: ``TRACKED_TASKS_MAX`` lets Jason loosen it without a redeploy.
DEFAULT_CAP = 20

#: Title character budget before truncation (ellipsis counts toward the
#: limit, so the rendered cap is ~70 chars including the "…").
TITLE_LIMIT = 70

# ``github-issue:<owner>/<repo>#<N>`` — the canonical shape the sync pass
# writes into ``tags``.
_GH_TAG_RE = re.compile(
    r"github-issue:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+", re.I
)
# ``<owner>/<repo>#<N>`` — the shape the ``source`` field sometimes
# carries (``jcronq/alice#17``).
_GH_SHORTREF_RE = re.compile(
    r"\b[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+", re.I
)
# ``https://github.com/.../issues/<N>`` — the full URL form that
# occasionally lands in ``source`` / ``merge_ref`` / tags.
_GH_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/\d+",
    re.I,
)


def _cap_from_env(env: dict[str, str] | None = None) -> int:
    """``DEFAULT_CAP``, overridable by ``TRACKED_TASKS_MAX``."""
    e = dict(os.environ if env is None else env)
    raw = (e.get("TRACKED_TASKS_MAX") or "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_CAP


def _parse_iso(value: object) -> float | None:
    """ISO-8601 (``Z`` or offset) → epoch seconds; ``None`` on junk."""
    s = str(value or "").strip().strip('"').strip("'")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_task_yaml(text: str) -> tuple[dict[str, str], list[str]]:
    """Return ``(scalars, tags)`` from a task.yaml.

    Deliberately minimal — we don't pull in PyYAML (``status_site`` is
    stdlib-only by design) and task files only carry top-level scalars
    plus one list (``tags``). Handles both flow-style (``tags: [a, b]``)
    and block-style (``tags:\\n  - a\\n  - b``) lists. Unknown or
    malformed structures degrade to empty values so one bad file can't
    crash the scan.
    """
    scalars: dict[str, str] = {}
    tags: list[str] = []
    in_tags_block = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(line) - len(stripped)
        if in_tags_block:
            # Block sequence items can live at the same indent as the
            # parent key (YAML 1.2 § 8.2.1), so we accept the ``-``
            # prefix regardless of indent. The first non-hyphen line
            # exits the block.
            if stripped.startswith("- ") or stripped == "-":
                tag = stripped[1:].strip().strip('"').strip("'")
                if tag:
                    tags.append(tag)
                i += 1
                continue
            in_tags_block = False
        if indent > 0:
            # Continuation / nested scalar we don't care about.
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        value = rest.strip()
        if key == "tags":
            if not value:
                in_tags_block = True
            else:
                # Flow style: ``[a, b, c]`` — strip brackets + split.
                flow = value.strip()
                if flow.startswith("[") and flow.endswith("]"):
                    inner = flow[1:-1]
                    for part in inner.split(","):
                        tag = part.strip().strip('"').strip("'")
                        if tag:
                            tags.append(tag)
            i += 1
            continue
        if key == "title" and value.startswith("'") and not value.endswith("'"):
            # YAML wrap-continuation for a quoted title: join follow-on
            # indented lines until the closing quote (task writers
            # occasionally produce ``'first line\n  rest'`` for long
            # titles). Falls through to normal parsing if no close quote
            # is found within a small window.
            buf = [value]
            j = i + 1
            while j < len(lines) and j - i < 10:
                nxt = lines[j]
                if not nxt.startswith(" "):
                    break
                buf.append(nxt.strip())
                if nxt.strip().endswith("'"):
                    j += 1
                    break
                j += 1
            joined = " ".join(buf).strip().strip("'")
            scalars[key] = joined
            i = j
            continue
        if value:
            scalars[key] = value.strip('"').strip("'")
        i += 1
    return scalars, tags


def _has_github_backing(
    scalars: dict[str, str], tags: Sequence[str]
) -> bool:
    """True when the task is already tracked via a GH issue.

    We check ``tags``, ``source``, and ``merge_ref`` for any of the
    three link shapes (``github-issue:owner/repo#N`` tag, bare
    ``owner/repo#N`` ref, or an ``https://github.com/.../issues/N`` URL).
    """
    for tag in tags:
        if _GH_TAG_RE.search(tag) or _GH_URL_RE.search(tag):
            return True
    for key in ("source", "merge_ref"):
        val = scalars.get(key) or ""
        if not val:
            continue
        if _GH_URL_RE.search(val):
            return True
        if _GH_SHORTREF_RE.search(val):
            return True
    return False


def _truncate(text: str, limit: int = TITLE_LIMIT) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def scan_tasks(
    tasks_dir: Path, *, now: float
) -> list[dict]:
    """Walk ``tasks_dir/task-*/task.yaml`` → list of tracked-task rows.

    Rows: ``{slug, title, state, actor, artifact_type, age_seconds,
    created_epoch, link}``. ``link`` is always ``""`` — there's no task
    detail page to point at yet. Age uses ``created`` with a fallback
    chain to ``updated`` then file mtime. Sorted oldest-first.

    Unreadable task.yaml files are skipped silently so one bad task
    can't poison the whole section. Terminal + GH-backed tasks are
    filtered out here, not downstream, so the resulting list is
    directly renderable.
    """
    out: list[dict] = []
    if not tasks_dir.is_dir():
        return out
    for d in sorted(tasks_dir.glob("task-*")):
        if not d.is_dir():
            continue
        f = d / "task.yaml"
        try:
            text = f.read_text(errors="replace")
            mtime = f.stat().st_mtime
        except OSError:
            continue
        scalars, tags = _parse_task_yaml(text)
        status = (scalars.get("status") or "").lower()
        if status in TERMINAL_STATUSES:
            continue
        if _has_github_backing(scalars, tags):
            continue
        ts = (
            _parse_iso(scalars.get("created"))
            or _parse_iso(scalars.get("updated"))
            or mtime
        )
        out.append(
            {
                "slug": scalars.get("id") or d.name,
                "title": scalars.get("title") or "",
                "state": status or "unknown",
                "actor": (scalars.get("actor") or "").lower(),
                "artifact_type": scalars.get("artifact_type") or "",
                "age_seconds": max(0.0, now - ts),
                "created_epoch": ts,
                "link": "",
            }
        )
    # Oldest first (largest age). Stable tiebreak by slug for determinism.
    out.sort(key=lambda r: (-r["age_seconds"], r["slug"]))
    return out


def _fmt_age(seconds: float) -> str:
    """Human age: ``45s`` / ``12m`` / ``3h`` / ``2d``.

    Matches the compact single-unit shape the Thinking queue uses so
    rows read consistently. Zero-clamped on negative skew.
    """
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    return f"{h // 24}d"


def render(
    tasks: Sequence[dict],
    *,
    now: datetime | None = None,
    cap: int | None = None,
) -> str:
    """Render the Tracked tasks region body (no heading, no markers).

    Shape per row::

        - <age> — **<slug>** <title> _(<actor> · <artifact_type>)_

    Overflow collapses to ``- +N more — see \\`task ls\\``` so the
    section stays bounded. Trailing staleness footer always present
    so the frontend's red-tint-on-stale hook works.
    """
    if cap is None:
        cap = _cap_from_env()
    if not tasks:
        body = "*(no tracked tasks)*"
    else:
        shown = list(tasks)[:cap]
        lines: list[str] = []
        for t in shown:
            age = _fmt_age(float(t.get("age_seconds") or 0.0))
            slug = str(t.get("slug") or "")
            title = _truncate(str(t.get("title") or ""))
            actor = str(t.get("actor") or "") or "?"
            atype = str(t.get("artifact_type") or "")
            meta_bits = [actor]
            if atype:
                meta_bits.append(atype)
            meta = " · ".join(meta_bits)
            parts = [f"- {age} — **{slug}**"]
            if title:
                parts.append(title)
            parts.append(f"_({meta})_")
            lines.append(" ".join(parts))
        overflow = max(0, len(tasks) - len(shown))
        if overflow:
            lines.append(
                f"- +{overflow} more — see `task ls` (SM v2 CLI)"
            )
        body = "\n".join(lines)
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)


# ─── live-source helper ─────────────────────────────────────────────


def tasks_dir_from_env(env: dict[str, str] | None = None) -> Path:
    """``~/alice-mind/inner/tasks``; overridable by ``ALICE_MIND``."""
    e = dict(os.environ if env is None else env)
    mind = e.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "tasks"


def typed_rows(tasks: Iterable[dict]) -> list[dict]:
    """Shape the SSE / typed consumers see.

    Same fields as :func:`scan_tasks` but excluding the internal
    ``created_epoch`` scratchpad. Kept as a distinct function so the
    public contract is explicit when the section grows widgets later.
    """
    out: list[dict] = []
    for t in tasks:
        out.append(
            {
                "slug": t.get("slug", ""),
                "title": t.get("title", ""),
                "state": t.get("state", ""),
                "actor": t.get("actor", ""),
                "artifact_type": t.get("artifact_type", ""),
                "age_seconds": float(t.get("age_seconds") or 0.0),
                "link": t.get("link", ""),
            }
        )
    return out
