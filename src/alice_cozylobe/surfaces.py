"""Surface + note emitters — the lobe's only writes into ~/alice-mind/.

Three channels, per the wake-loop design + issue #411 noise routing:

* **observation note** — :func:`write_observation_note` drops a
  fleeting note into ``~/alice-mind/inner/notes/`` with
  ``tag: lobe-observation``. Thinking's drain promotes them to the
  vault. This is the default channel for HIGH / MEDIUM / LOW events
  AND anything not classified as low-value sensor telemetry.
* **noise note** — :func:`write_noise_note` drops a fleeting note
  into ``~/alice-mind/inner/notes/noise/``. Same shape as observation
  notes but the subdirectory is invisible to thinking's inbox-drain
  trigger (non-recursive iterdir / glob in
  ``alice_thinking.vault_state._has_pending_inbox`` /
  ``alice_thinking.phase``). Used for low-value sensor telemetry —
  motion-pipeline batches, light_level, ambient temp/humidity —
  per the design in
  ``cortex-memory/research/2026-05-25-cozylobe-noise-inbox-routing-design.md``.
* **urgent surface** — :func:`write_urgent_surface` drops a file into
  ``~/alice-mind/inner/surface/`` with the
  ``cozylobe-urgent-<slug>`` naming convention. Speaking's surface
  watcher picks these up on its next poll and routes them through
  the address book. Reserved for CRITICAL events (or HIGH events the
  agent explicitly marks urgent).

All three helpers are pure-Python file emitters — no MCP, no shell.
The ``inner/notes/`` write path is identical to what
:mod:`alice_speaking.tools.append_note` produces for thinking; we
duplicate the shape here so the lobe doesn't take an alice_speaking
import.
"""

from __future__ import annotations

import logging
import pathlib
import re
import time
from typing import Iterable, Optional


__all__ = [
    "DEFAULT_MIND",
    "build_slug",
    "write_noise_note",
    "write_observation_note",
    "write_urgent_surface",
]


log = logging.getLogger(__name__)

DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")

# Matches the timestamp prefix the rest of alice uses for fleeting notes
# (six-digit HHMMSS local time after a YYYY-MM-DD date). Same convention
# as alice_thinking's vault writes so the cue runner / drain doesn't
# need to special-case the lobe's output.
_TS_FMT_DATE = "%Y-%m-%d"
_TS_FMT_TIME = "%H%M%S"


_SLUG_SANITIZE = re.compile(r"[^a-z0-9]+")


def build_slug(*parts: str, max_length: int = 60) -> str:
    """Build a filesystem-safe slug from arbitrary string parts.

    Joins parts with ``-``, lowercases, replaces non-alphanumerics with
    ``-``, collapses runs, trims to ``max_length``. Empty parts and
    leading/trailing dashes are dropped. Returns ``"event"`` as a
    fallback for empty input so we never emit a bare timestamp.
    """
    joined = "-".join(str(p) for p in parts if p)
    slug = _SLUG_SANITIZE.sub("-", joined.lower()).strip("-")
    if not slug:
        return "event"
    return slug[:max_length].strip("-") or "event"


def _utc_prefix(now: Optional[float] = None) -> str:
    """Return the ``YYYY-MM-DD-HHMMSS`` prefix the other channels use."""
    ts = now if now is not None else time.time()
    lt = time.gmtime(ts)
    return f"{time.strftime(_TS_FMT_DATE, lt)}-{time.strftime(_TS_FMT_TIME, lt)}"


def write_observation_note(
    body: str,
    *,
    slug: str,
    tags: Iterable[str] = ("lobe-observation",),
    mind: Optional[pathlib.Path] = None,
    now: Optional[float] = None,
) -> pathlib.Path:
    """Drop a fleeting observation note into ``inner/notes/``.

    Frontmatter carries ``created`` + ``tags`` + ``source: cozylobe``
    so thinking's drain can route it correctly. Returns the absolute
    path written. The directory is created if missing — first-boot the
    inner/ tree may be empty on a fresh install.

    ``mind`` defaults to :data:`DEFAULT_MIND` resolved at call time so
    tests can monkeypatch the module-level constant. (Binding it as a
    default-arg value would freeze it at import time.)
    """
    if mind is None:
        mind = DEFAULT_MIND
    notes_dir = mind / "inner" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    prefix = _utc_prefix(now)
    filename = f"{prefix}-cozylobe-{slug}.md"
    path = notes_dir / filename
    tag_list = ", ".join(sorted(set(tags)))
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(now if now is not None else time.time())
    )
    content = (
        "---\n"
        f"created: {created}\n"
        "source: cozylobe\n"
        f"tags: [{tag_list}]\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )
    path.write_text(content)
    log.info("cozylobe note: %s", path)
    return path


def write_noise_note(
    body: str,
    *,
    slug: str,
    tags: Iterable[str] = ("lobe-observation", "noise"),
    mind: Optional[pathlib.Path] = None,
    now: Optional[float] = None,
) -> pathlib.Path:
    """Drop a noise-class fleeting note into ``inner/notes/noise/``.

    Issue #411: motion-pipeline batches, light_level / ambient / humidity
    telemetry, and other low-value sensor events are written here instead
    of ``inner/notes/`` so they don't keep thinking's inbox perpetually
    non-empty. The subdirectory is invisible to thinking's drain
    triggers (non-recursive iterdir + glob), so noise notes accumulate
    without gating Stage D fires.

    The body / frontmatter shape is identical to
    :func:`write_observation_note` so the data shape is unchanged —
    only the path differs. Thinking can still walk noise/ manually
    when auditing home-behavior patterns (the cortex-memory skill
    treats it as a regular folder; nothing's hidden from a vault read).

    Tags default to ``lobe-observation`` + ``noise`` so a misrouted
    note remains tagged as a lobe observation; the ``noise`` tag lets
    thinking-side queries identify it as a noise-class write if it
    ever does walk the directory.

    ``mind`` defaults to :data:`DEFAULT_MIND` resolved at call time so
    tests can monkeypatch the module-level constant.
    """
    if mind is None:
        mind = DEFAULT_MIND
    noise_dir = mind / "inner" / "notes" / "noise"
    noise_dir.mkdir(parents=True, exist_ok=True)
    prefix = _utc_prefix(now)
    filename = f"{prefix}-cozylobe-{slug}.md"
    path = noise_dir / filename
    tag_list = ", ".join(sorted(set(tags)))
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(now if now is not None else time.time())
    )
    content = (
        "---\n"
        f"created: {created}\n"
        "source: cozylobe\n"
        "route: noise\n"
        f"tags: [{tag_list}]\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )
    path.write_text(content)
    log.info("cozylobe noise note: %s", path)
    return path


def write_urgent_surface(
    body: str,
    *,
    slug: str,
    surface_type: str = "cozylobe-urgent",
    mind: Optional[pathlib.Path] = None,
    now: Optional[float] = None,
    extra_frontmatter: Optional[dict] = None,
) -> pathlib.Path:
    """Drop a surface file into ``inner/surface/`` for speaking to pick up.

    Reserved for CRITICAL events and HIGH events the agent marks
    urgent. Returns the absolute path written. The shape matches the
    surface convention thinking uses (see
    :func:`alice_thinking.design_pipeline.write_surface`) so speaking's
    watcher doesn't need a cozylobe-specific code path.
    """
    if mind is None:
        mind = DEFAULT_MIND
    surface_dir = mind / "inner" / "surface"
    surface_dir.mkdir(parents=True, exist_ok=True)
    prefix = _utc_prefix(now)
    filename = f"{prefix}-{surface_type}-{slug}.md"
    path = surface_dir / filename
    extra = extra_frontmatter or {}
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(now if now is not None else time.time())
    )
    fm_lines = [
        "---",
        f"created: {created}",
        "source: cozylobe",
        f"surface_type: {surface_type}",
    ]
    for key, value in sorted(extra.items()):
        fm_lines.append(f"{key}: {value}")
    fm_lines.append("---")
    content = "\n".join(fm_lines) + "\n\n" + body.rstrip() + "\n"
    path.write_text(content)
    log.info("cozylobe surface: %s", path)
    return path
