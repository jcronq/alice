"""Waiting-on-you (``kind=woy``) tasks — structured feed for the WoY tier.

Jason's intake-stays-private rule means WoY items can't ride on GitHub
issues (a public repo would leak personal errands like "Call Randy
Sosa at NAM"). SM v2 tasks are the private structured primitive we
already use for internal tracking; this source extends them with five
optional scalars (``kind``, ``verb``, ``target``, ``context``,
``external``) and adapts them to the shape ``_render_waiting_on_you``
and ``_render_your_todo`` need.

A row carries::

    {
        "task_id":          "task-NNNN",
        "verb":             "Call",
        "target":           "Randy Sosa at NAM",
        "context":          "<context sentence>",
        "body":             "<context sentence>",     # alias
        "title":            "Call Randy Sosa at NAM",
        "slug":             "call-randy-sosa-at-nam", # matches titleSlug
        "accept_descriptors": [...],
        "task_id":          "task-NNNN",
        "external":         true,
        "status":           "draft",
        "snooze_until":     None | ISO-8601,
        "age_seconds":      <float>,
    }

The renderer also emits a hand-zone-shaped markdown blob via
:func:`render_markdown` so the existing ``WaitingTable`` widget renders
the items alongside any surviving Jason-authored lines, and the server
splits the slugs into ``needs_approval_slugs`` (chip_set=approval →
Accept / Reject) vs. ``todo_chip_slugs`` (chip_set=todo, the default →
Done / Snooze) so the matching chip pair attaches to each row.

Stdlib-only by convention (``status_site.sources.tasks`` sets this
precedent). YAML parsing reuses :func:`status_site.sources.tasks._parse_task_yaml`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from status_site.history import title_slug
from status_site.sources import tasks as _tasks  # noqa: F401 — kept for test parity

SOURCE_NAME = "woy-tasks"

#: Statuses that keep a kind=woy task visible. ``validating`` /
#: ``blocked`` would be odd for an external action but we don't rule
#: them out — the UI should still see the item so Jason can intervene.
WOY_VISIBLE_STATUSES: frozenset[str] = frozenset(
    {"draft", "selected", "building", "reviewing", "validating", "blocked"}
)


def _parse_iso(value: object) -> Optional[float]:
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


#: Default chip set for ``kind=woy`` tasks when none was set on the
#: task.yaml. The default is ``"todo"`` (Done / Snooze) because the
#: overwhelming majority of WoY items are simple action-TODOs Jason
#: will either do or defer — "reject" is nonsensical for "Pull dead
#: drive sdb from NAS". ``"approval"`` opts a row into Accept / Reject
#: for the design-review subcase.
DEFAULT_CHIP_SET = "todo"

#: Valid ``chip_set`` values. A task.yaml carrying an unknown value
#: falls back to :data:`DEFAULT_CHIP_SET` (the renderer is forgiving
#: because the YAML is user-editable); the store-level validation in
#: :func:`alice_forge.task_store.TaskStore.create` is where the
#: write path refuses unknown input.
VALID_CHIP_SETS: frozenset[str] = frozenset({"todo", "approval"})


def _normalize_chip_set(value: object) -> str:
    """Coerce a raw chip_set scalar to one of :data:`VALID_CHIP_SETS`.

    Unknown / missing values fall back to :data:`DEFAULT_CHIP_SET`.
    The renderer never raises on a stale YAML; the write path owns
    validation.
    """
    if value is None:
        return DEFAULT_CHIP_SET
    s = str(value).strip().lower()
    return s if s in VALID_CHIP_SETS else DEFAULT_CHIP_SET


def _descriptors_for_chip_set(task_id: str, chip_set: str) -> list[dict]:
    """Return the ``accept_descriptors`` trio for one ``chip_set``.

    The shape matches ``rows[].accept_descriptors`` promised in the
    design: ``{key, label, action, variant}``. Downstream code (the
    shell chip renderer and the ``/api/woy/action`` handler) keys off
    the ``action`` field.

    * ``"todo"`` (default) → **Done** + **Snooze**. No reject chip —
      cancelling a WoY task is a rare one-off, reachable via
      ``task reject task-NNNN`` on the CLI.
    * ``"approval"`` → **Accept** + **Reject**. Used for the
      design-review subcase where Alice drafted something and needs
      explicit sign-off before the outcome ships.
    """
    if chip_set == "approval":
        return [
            {"key": "accept", "label": "Accept", "action": "task-close", "variant": "primary"},
            {"key": "reject", "label": "Reject", "action": "task-reject", "variant": "danger"},
        ]
    # Default + unknown → todo semantics.
    return [
        {"key": "done", "label": "Done", "action": "task-close", "variant": "primary"},
        {"key": "snooze", "label": "Snooze", "action": "task-snooze", "variant": "secondary"},
    ]


# Back-compat alias retained for any importer that still calls the
# legacy name. The new call site uses :func:`_descriptors_for_chip_set`
# with an explicit ``chip_set`` so tests can lock the renaming in.
def _default_accept_descriptors(task_id: str) -> list[dict]:
    return _descriptors_for_chip_set(task_id, DEFAULT_CHIP_SET)


def _is_snoozed(snooze_until: Optional[str], now_epoch: float) -> bool:
    """True when ``snooze_until`` parses to a timestamp after ``now``."""
    if not snooze_until:
        return False
    ts = _parse_iso(snooze_until)
    if ts is None:
        return False
    return ts > now_epoch


def _title_from(verb: Optional[str], target: Optional[str], fallback_title: str) -> str:
    """Build ``"{verb} {target}"`` falling back to the raw task title."""
    bits = [b for b in [(verb or "").strip(), (target or "").strip()] if b]
    if bits:
        return " ".join(bits)
    return fallback_title.strip()


def _load_task_scalars_from_text(text: str) -> Optional[dict]:
    """Parse a task.yaml body, preferring PyYAML for multi-line scalars.

    ``status_site`` is nominally stdlib-only, but ``alice_forge.task_store``
    is bind-mounted into the status-site container specifically so the WoY
    source can round-trip the full ``context`` field (block-folded scalars
    the minimal parser would truncate — see :mod:`status_site.sources.tasks`).

    Falls back to :func:`status_site.sources.tasks._parse_task_yaml` when
    PyYAML isn't importable — the row stays serviceable but a multi-line
    ``context`` may truncate.
    """
    try:
        import yaml as _yaml  # type: ignore
    except ImportError:
        _yaml = None  # type: ignore
    if _yaml is not None:
        try:
            data = _yaml.safe_load(text)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 — fall back below
            pass
    # Last-resort: the minimal parser from ``sources.tasks``. Multi-line
    # scalars (block-folded ``context`` values) truncate to the first line
    # here; the PyYAML path above handles them correctly.
    scalars, _tags = _tasks._parse_task_yaml(text)
    return dict(scalars) if scalars else None


def _load_task_scalars(f: Path) -> Optional[dict]:
    """File → :func:`_load_task_scalars_from_text`. Returns ``None`` on
    IO errors so the caller can skip the row silently."""
    try:
        text = f.read_text(errors="replace")
    except OSError:
        return None
    return _load_task_scalars_from_text(text)


#: Regex used by the fast pre-filter in :func:`scan_woy_tasks` — the
#: canonical YAML shape ``alice_forge.task_store`` emits is
#: ``kind: woy`` on its own line. Keeping the match anchored on the
#: ``kind:`` key (vs. a bare ``woy`` search) avoids spurious hits
#: from body copy (e.g. a ``context`` field that mentions ``WoY``).
_KIND_WOY_LINE = "\nkind: woy\n"


def _is_woy_task_fast(text: str) -> bool:
    """Cheap pre-filter: True when the YAML body contains ``kind: woy``.

    Avoids invoking the YAML parser on the ~1100 non-woy tasks that
    dominate the store (benchmarked at ~960ms for a full-tree parse
    vs. ~45ms for the string-scan). False positives are benign —
    they just fall through to the full parse which drops them.
    """
    # Tolerate the leading-line case where the file starts with
    # ``kind: woy`` (no preceding newline).
    if text.startswith("kind: woy\n"):
        return True
    return _KIND_WOY_LINE in text


def scan_woy_tasks(
    tasks_dir: Path, *, now: float, include_snoozed: bool = False
) -> list[dict]:
    """Walk ``tasks_dir`` → list of kind=woy rows.

    Filters:

    * ``kind == "woy"`` (anything else → skipped; the Tracked-tasks
      section owns the rest).
    * ``status`` in :data:`WOY_VISIBLE_STATUSES` (terminal statuses and
      ``designing`` / ``designed`` hide).
    * ``snooze_until > now`` → hidden unless ``include_snoozed=True``.

    Sorted oldest-first by ``created`` (falling back to ``updated`` then
    mtime) so Jason's older asks stay visible at the top of the list.

    A text pre-filter (:func:`_is_woy_task_fast`) skips the YAML parse
    for every task that doesn't carry ``kind: woy`` on its own line.
    This keeps the SSE tick cheap even with ~1100+ tasks in the store.
    """
    if not tasks_dir.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(tasks_dir.glob("task-*")):
        if not d.is_dir():
            continue
        f = d / "task.yaml"
        try:
            mtime = f.stat().st_mtime
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if not _is_woy_task_fast(text):
            continue
        scalars = _load_task_scalars_from_text(text)
        if not scalars:
            continue
        if str(scalars.get("kind") or "").lower() != "woy":
            continue
        status = str(scalars.get("status") or "").lower()
        if status not in WOY_VISIBLE_STATUSES:
            continue
        snooze_raw = scalars.get("snooze_until")
        snooze_until = str(snooze_raw).strip() if snooze_raw else None
        if snooze_until == "":
            snooze_until = None
        if not include_snoozed and _is_snoozed(snooze_until, now):
            continue
        verb = str(scalars.get("verb") or "").strip() or None
        target = str(scalars.get("target") or "").strip() or None
        context = str(scalars.get("context") or "").strip() or None
        title_text = _title_from(verb, target, str(scalars.get("title") or ""))
        slug = title_slug(title_text) if title_text else ""
        ts = (
            _parse_iso(scalars.get("created"))
            or _parse_iso(scalars.get("updated"))
            or mtime
        )
        external_raw = scalars.get("external")
        if isinstance(external_raw, bool):
            external = external_raw
        elif external_raw is None:
            external = True
        else:
            external = str(external_raw).strip().lower() in {"1", "true", "yes", "y"}
        task_id = str(scalars.get("id") or d.name).strip()
        chip_set = _normalize_chip_set(scalars.get("chip_set"))
        row = {
            "task_id": task_id,
            "verb": verb or "",
            "target": target or "",
            "context": context or "",
            "body": context or "",
            "title": title_text,
            "slug": slug,
            "chip_set": chip_set,
            "accept_descriptors": _descriptors_for_chip_set(task_id, chip_set),
            "external": external,
            "status": status,
            "snooze_until": snooze_until,
            "age_seconds": max(0.0, now - ts),
            "created_epoch": ts,
        }
        out.append(row)
    # Oldest first.
    out.sort(key=lambda r: (-r["age_seconds"], r["task_id"]))
    return out


def render_markdown(rows: Sequence[dict]) -> str:
    """Build the hand-zone markdown spliced into WoY's ``your_todo_md``.

    Each row renders as ``- [ ] **{verb} {target}**: {context}``. The
    bold span's text slugifies (via ``titleSlug``) to the same string
    this source puts on ``row['slug']``, so Slice-2's chip / dismissal
    plumbing attaches without extra rewiring.

    Rows with no context collapse to ``- [ ] **{verb} {target}**``.
    Empty input → empty string (caller decides whether to omit the
    section entirely).
    """
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        title = str(r.get("title") or "").strip()
        if not title:
            # Degrade: fall back to a bare body so the item still
            # renders; the slug is empty, so chip actions skip it.
            body = str(r.get("body") or r.get("context") or "").strip()
            if body:
                lines.append(f"- [ ] {body}")
            continue
        context = str(r.get("context") or r.get("body") or "").strip()
        if context:
            lines.append(f"- [ ] **{title}**: {context}")
        else:
            lines.append(f"- [ ] **{title}**")
    return "\n".join(lines)


def slug_to_task_id(tasks_dir: Path, slug: str) -> Optional[str]:
    """Reverse lookup: WoY slug → task id (or ``None`` when no match).

    Used by ``handle_woy_action`` to resolve an incoming chip click
    back to the SM task it was rendered from. Walks every ``kind=woy``
    task (including snoozed ones) because a steer-click on a snoozed
    item should still find its backing task.
    """
    if not slug:
        return None
    rows = scan_woy_tasks(tasks_dir, now=0.0, include_snoozed=True)
    for r in rows:
        if r.get("slug") == slug:
            return r.get("task_id")
    return None


def typed_rows(rows: Iterable[dict]) -> list[dict]:
    """Shape the typed-rows the SSE snapshot carries.

    Mirrors :func:`scan_woy_tasks` output minus ``age_seconds`` (which
    drifts every tick and would fire spurious section.update events;
    consumers that want "how old" can derive it from ``created_epoch``
    themselves) and the internal ``created_epoch`` scratchpad field.
    """
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "task_id": r.get("task_id", ""),
                "verb": r.get("verb", ""),
                "target": r.get("target", ""),
                "context": r.get("context", ""),
                "body": r.get("body", r.get("context", "")),
                "title": r.get("title", ""),
                "slug": r.get("slug", ""),
                "chip_set": r.get("chip_set", DEFAULT_CHIP_SET),
                "accept_descriptors": list(r.get("accept_descriptors") or []),
                "external": bool(r.get("external")),
                "status": r.get("status", ""),
                "snooze_until": r.get("snooze_until"),
                "created_epoch": float(r.get("created_epoch") or 0.0),
            }
        )
    return out
