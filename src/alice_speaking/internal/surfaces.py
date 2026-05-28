"""Surface watcher — internal source for thinking-Alice's surfaced thoughts.

A *surface* is a markdown file dropped into ``inner/surface/`` by the
thinking hemisphere when it wants to voice a thought to the speaking
hemisphere. The watcher polls that directory, queues each new file as
a :class:`SurfaceEvent`, and the dispatcher routes it through this
class's :meth:`handle` to the existing
:func:`alice_speaking._dispatch.handle_surface`.

Phase 5 of plan 01 owns the loop and the dispatched-set bookkeeping
end-to-end. State that's truly per-watcher (which surfaces have
already been pushed onto the queue, which directory we're watching)
lives on the instance. State that's still daemon-shared
(``_archive_unresolved``, the handled directory used by archive)
stays reachable via the daemon-proxy ``ctx`` until Phase 6 extracts
those services.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Optional

from ..transports.base import DaemonContext


log = logging.getLogger(__name__)


# Poll interval for both the surface and emergency watchers. Five
# seconds: short enough that a freshly-written .md surface dispatches
# in human-perceivable time, long enough that an idle daemon doesn't
# burn cycles on `glob("*.md")` every loop.
POLL_SECONDS = 5.0


# Liveness file the container HEALTHCHECK probes for speaking-side wedges.
# Refreshed every SurfaceWatcher._run tick — that's the fixed-cadence
# event-loop wake that fires regardless of inbound traffic, so a hung turn
# (or a hung kernel) shows up as a stuck mtime within ~6 min (5-min staleness
# window + interval slack). The touch site swallows ONLY FileNotFoundError
# so dev/test environments (where /state/worker/ doesn't exist) can import
# and exercise the watcher as a no-op; in prod the directory is guaranteed
# by sandbox/s6/init-state-perms, and a genuinely missing file is caught by
# the HEALTHCHECK's mtime-staleness test. Other OSErrors (PermissionError,
# read-only FS, etc.) are NOT swallowed — those are real failures we want
# surfaced. See sandbox/Dockerfile HEALTHCHECK comment.
SPEAKING_LIVENESS_PATH = pathlib.Path("/state/worker/speaking-alive")


def _touch_liveness(path: pathlib.Path) -> None:
    """Touch the speaking-side liveness file so the container HEALTHCHECK
    sees a fresh mtime. Extracted as a function so the unit test can pass
    a tmp_path override. Swallows FileNotFoundError only (parent dir
    missing) for dev/test environments; other OSErrors propagate."""
    try:
        path.touch()
    except FileNotFoundError:
        pass


# Surface types that are produced by automated, cron-driven scans
# (not LLM judgment) and tend to re-fire identical signal multiple
# times per day. The first occurrence of each type per day dispatches
# normally; subsequent same-type surfaces on the same day are
# auto-archived without dispatching. Add a type here when it has
# demonstrated noise: re-firing the same payload more than ~3 times
# in a day with no new action for me to take.
DEDUPE_BY_TYPE_PER_DAY: frozenset[str] = frozenset({
    "stage-d-invariant",
})


_SURFACE_TYPE_RE = re.compile(r"^surface_type:\s*(\S+)\s*$", re.MULTILINE)
_SOURCE_ID_RE = re.compile(r"^source-id:\s*(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_DATE_RE = re.compile(r"^date:\s*(\S+)\s*$", re.MULTILINE)
_FILENAME_DATE_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-\d{6}-(.+)$")
_VIOLATION_COUNT_RE = re.compile(r"^violation_count:\s*(\d+)\s*$", re.MULTILINE)

# Rolling window for id-based intake dedup: if the same key appears
# within this many hours, the second occurrence is suppressed.
ID_DEDUP_WINDOW_HOURS = 24

# Floor on the |violation_count delta| between a new stage-d-invariant
# surface and the most recent prior of the same type on the same day.
# Below this floor the new surface is auto-archived as noise; at or
# above it the surface dispatches normally so the operator sees the
# meaningful change. 5 is empirically chosen — a Stage D scan
# incrementing by 1-2 unaudited notes since the last firing is grooming
# churn; 5+ likely represents a real burst worth surfacing.
STAGE_D_INVARIANT_DELTA_FLOOR = 5


@dataclass
class SurfaceEvent:
    """A new ``inner/surface/<id>.md`` file ready to dispatch.

    Lives next to :class:`SurfaceWatcher`. Re-exported from
    ``alice_speaking.daemon`` for back-compat with existing test
    imports.
    """

    path: pathlib.Path


class SurfaceWatcher:
    """Internal-source wrapper for surface dispatch.

    Owns the poll loop (:meth:`producer`) and the dispatched-set
    bookkeeping that makes one-surface-one-turn work end-to-end:
    every dispatched filename lands in :attr:`_dispatched`, and
    :meth:`handle` removes it from that set in a ``finally`` block
    so a failed handler doesn't permanently shadow the surface.
    """

    name = "surfaces"
    event_type = SurfaceEvent

    def __init__(self, mind_dir: pathlib.Path) -> None:
        self._surface_dir = mind_dir / "inner" / "surface"
        self._handled_dir = self._surface_dir / ".handled"
        self._dedup_log_path = (
            mind_dir / "inner" / "state" / "surface-intake-dedup.jsonl"
        )
        self._dispatched: set[str] = set()

    @property
    def surface_dir(self) -> pathlib.Path:
        """Public access for :func:`_dispatch.handle_surface` and the
        daemon's archive helper, both of which still need the path."""
        return self._surface_dir

    @property
    def handled_dir(self) -> pathlib.Path:
        return self._handled_dir

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule the watch loop. Returns the task so the daemon
        can supervise it under the same start/cancel semantics as a
        transport's producer."""
        return asyncio.create_task(self._run(ctx), name="sur-produce")

    async def _run(self, ctx: DaemonContext) -> None:
        """Poll ``inner/surface/`` for new ``*.md`` files, push each
        as a :class:`SurfaceEvent` onto the dispatcher queue.

        Flat-file only — files in subdirectories of ``inner/surface/``
        (other than ``.handled/``) will not be picked up. A one-shot
        drift check at startup warns when surfaces have been stranded
        in subdirs so the operator notices.
        """
        self._surface_dir.mkdir(parents=True, exist_ok=True)
        self._handled_dir.mkdir(parents=True, exist_ok=True)
        for entry in self._surface_dir.iterdir():
            if entry.is_dir() and entry.name not in (".handled",):
                md_count = len(list(entry.glob("*.md")))
                if md_count:
                    log.warning(
                        "surface drift: %d .md file(s) in subdir %s — "
                        "the watcher is non-recursive; these will not "
                        "dispatch. Move them to flat inner/surface/ "
                        "format.",
                        md_count,
                        entry.name,
                    )
        while not ctx._stop.is_set():
            # Liveness heartbeat — this loop is the speaking event-loop
            # tick that wakes regardless of inbound traffic, which is
            # exactly the signal we want the container HEALTHCHECK to
            # observe. Touch first so a downstream OSError on the glob
            # doesn't skip the heartbeat. NOT wrapped in try/except: if
            # /state/worker isn't writable, the probe SHOULD fire.
            _touch_liveness(SPEAKING_LIVENESS_PATH)
            try:
                for path in sorted(self._surface_dir.glob("*.md")):
                    if path.name.startswith(".") or path.name in self._dispatched:
                        continue
                    if self._suppress_as_duplicate(path):
                        continue
                    if self._suppress_as_id_duplicate(path):
                        continue
                    self._dispatched.add(path.name)
                    log.info("surface detected: %s", path.name)
                    await ctx._queue.put(SurfaceEvent(path=path))
            except OSError as exc:
                log.warning("surface poll error: %s", exc)
            await asyncio.sleep(POLL_SECONDS)

    def _suppress_as_duplicate(self, path: pathlib.Path) -> bool:
        """Auto-archive same-type-same-day duplicates of automated
        cron-driven surfaces without dispatching.

        Returns True iff the surface was suppressed. The first
        occurrence of a dedupe-eligible type on a given day still
        dispatches normally — only later same-type surfaces on the
        same day get archived with a duplicate-suppressed verdict.

        A surface is dedupe-eligible when its ``surface_type``
        frontmatter value is in :data:`DEDUPE_BY_TYPE_PER_DAY`.
        Missing or unparseable frontmatter → not eligible → falls
        through to normal dispatch.

        When both the new surface and the most recent same-type prior
        carry a ``violation_count`` frontmatter field, the
        :data:`STAGE_D_INVARIANT_DELTA_FLOOR` gate decides: deltas
        below the floor are noise (auto-archive with a
        ``let-pass (count-delta below floor)`` verdict), deltas at or
        above the floor are real signal and dispatch normally.
        Without a count on either side the surface falls back to the
        unconditional same-type-same-day suppression — that's the
        behaviour added in #79, preserved for any type where we have
        no scalar to read.
        """
        surface_type = _read_surface_type(path)
        if surface_type is None or surface_type not in DEDUPE_BY_TYPE_PER_DAY:
            return False
        today = datetime.date.today().isoformat()
        dest_dir = self._handled_dir / today
        if not dest_dir.is_dir():
            return False
        prior = self._latest_same_type_prior(dest_dir, surface_type, path.name)
        if prior is None:
            return False
        new_count = _read_violation_count(path)
        prior_count = _read_violation_count(prior)
        if new_count is not None and prior_count is not None:
            delta = abs(new_count - prior_count)
            if delta >= STAGE_D_INVARIANT_DELTA_FLOOR:
                return False
            self._archive_count_delta_duplicate(
                path, surface_type, prior.name, delta, new_count, prior_count
            )
            return True
        self._archive_duplicate(path, surface_type, prior.name)
        return True

    def _latest_same_type_prior(
        self,
        dest_dir: pathlib.Path,
        surface_type: str,
        exclude_name: str,
    ) -> Optional[pathlib.Path]:
        """Return the alphabetically-latest same-type prior in
        ``dest_dir`` (filenames embed ``HHMMSS`` so lex order == time
        order for a given day), or ``None`` if no prior matches."""
        latest: Optional[pathlib.Path] = None
        for candidate in dest_dir.glob("*.md"):
            if candidate.name == exclude_name:
                continue
            if _read_surface_type(candidate) != surface_type:
                continue
            if latest is None or candidate.name > latest.name:
                latest = candidate
        return latest

    def _archive_duplicate(
        self,
        path: pathlib.Path,
        surface_type: str,
        prior_name: str,
    ) -> None:
        """Move a same-type-same-day duplicate into the handled dir
        with a verdict naming the prior occurrence."""
        today = datetime.date.today().isoformat()
        dest_dir = self._handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + f"verdict: duplicate-suppressed-by-intake — surface_type '{surface_type}' already dispatched today ({prior_name})\n"
            + "action_taken: auto-archived by intake dedupe filter\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info(
            "surface dedupe: suppressed %s (surface_type=%s, prior=%s)",
            path.name,
            surface_type,
            prior_name,
        )

    def _archive_count_delta_duplicate(
        self,
        path: pathlib.Path,
        surface_type: str,
        prior_name: str,
        delta: int,
        new_count: int,
        prior_count: int,
    ) -> None:
        """Archive a same-type-same-day surface whose violation_count
        moved less than :data:`STAGE_D_INVARIANT_DELTA_FLOOR` since the
        prior dispatch — too small to bother surfacing."""
        today = datetime.date.today().isoformat()
        dest_dir = self._handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        verdict = (
            "let-pass (count-delta below floor) — "
            f"surface_type '{surface_type}' violation_count "
            f"{prior_count} → {new_count} (delta {delta} < "
            f"{STAGE_D_INVARIANT_DELTA_FLOOR}); prior: {prior_name}"
        )
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + f"verdict: {verdict}\n"
            + "action_taken: auto-archived by intake count-delta filter\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info(
            "surface count-delta dedupe: suppressed %s "
            "(surface_type=%s, prior=%s, delta=%d)",
            path.name,
            surface_type,
            prior_name,
            delta,
        )

    def _suppress_as_id_duplicate(self, path: pathlib.Path) -> bool:
        """Suppress same-id same-day re-issues using a JSONL state log.

        Computes a dedup key (``source-id`` or filename slug, plus the
        date) and consults ``inner/state/surface-intake-dedup.jsonl``
        for a prior occurrence within the last
        :data:`ID_DEDUP_WINDOW_HOURS`. If the prior is already in
        ``.handled/`` the new surface is auto-resolved with a
        ``let-pass (intake-side dedup)`` verdict pointing at the prior.
        If the prior is still pending the new surface is deferred —
        archived with a ``deferred-pending-prior`` verdict so it doesn't
        spawn duplicate work.

        Surfaces with no extractable key (no frontmatter id and an
        unrecognised filename pattern) fall through to no-dedup.

        Returns True iff the surface was suppressed.
        """
        key = _dedup_key(path)
        if key is None:
            return False
        now = datetime.datetime.now().astimezone()
        prior = self._lookup_prior_entry(key, now)
        if prior is None:
            self._record_dedup_entry(key, path.name, now)
            return False
        prior_filename = str(prior.get("filename") or "<unknown>")
        handled_path = self._find_in_handled(prior_filename)
        if handled_path is not None:
            self._archive_id_duplicate(
                path,
                key=key,
                prior_filename=prior_filename,
                handled_path=handled_path,
                deferred=False,
            )
        else:
            self._archive_id_duplicate(
                path,
                key=key,
                prior_filename=prior_filename,
                handled_path=None,
                deferred=True,
            )
        return True

    def _lookup_prior_entry(
        self, key: str, now: datetime.datetime
    ) -> Optional[dict]:
        """Return the most recent dedup-log entry with this key within
        the rolling window, or None if nothing matches."""
        if not self._dedup_log_path.is_file():
            return None
        cutoff = now - datetime.timedelta(hours=ID_DEDUP_WINDOW_HOURS)
        latest: Optional[dict] = None
        try:
            with self._dedup_log_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("key") != key:
                        continue
                    ts_raw = entry.get("ts")
                    if not isinstance(ts_raw, str):
                        continue
                    try:
                        ts = datetime.datetime.fromisoformat(ts_raw)
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.astimezone()
                    if ts < cutoff:
                        continue
                    latest = entry
        except OSError as exc:
            log.warning("dedup log read failed: %s", exc)
            return None
        return latest

    def _record_dedup_entry(
        self, key: str, filename: str, now: datetime.datetime
    ) -> None:
        """Append a new dedup-log entry for a surface that passed both
        intake filters. Survives daemon restart because the log lives
        on disk."""
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "key": key,
            "filename": filename,
        }
        try:
            self._dedup_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._dedup_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("dedup log write failed: %s", exc)

    def _find_in_handled(self, filename: str) -> Optional[pathlib.Path]:
        """Look for ``filename`` under any dated ``.handled/`` subdir.
        Returns the path if found, else None."""
        if not self._handled_dir.is_dir():
            return None
        try:
            for entry in self._handled_dir.iterdir():
                if not entry.is_dir():
                    continue
                candidate = entry / filename
                if candidate.exists():
                    return candidate
        except OSError as exc:
            log.warning("handled-dir scan failed: %s", exc)
            return None
        return None

    def _archive_id_duplicate(
        self,
        path: pathlib.Path,
        *,
        key: str,
        prior_filename: str,
        handled_path: Optional[pathlib.Path],
        deferred: bool,
    ) -> None:
        """Archive an id-dedup duplicate under today's handled dir with
        a verdict line referencing the prior occurrence."""
        today = datetime.date.today().isoformat()
        dest_dir = self._handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        if deferred:
            verdict = (
                "deferred-pending-prior (intake-side dedup) — "
                f"key '{key}' prior surface {prior_filename} still pending"
            )
            action = (
                "auto-archived by intake id-dedup filter; prior surface "
                f"{prior_filename} is still pending dispatch"
            )
        else:
            assert handled_path is not None
            handled_ref = f".handled/{handled_path.parent.name}/{handled_path.name}"
            verdict = (
                "let-pass (intake-side dedup) — "
                f"key '{key}' already resolved at {handled_ref}"
            )
            action = f"auto-archived by intake id-dedup filter; prior at {handled_ref}"
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + f"verdict: {verdict}\n"
            + f"action_taken: {action}\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info(
            "surface id-dedup: suppressed %s (key=%s, prior=%s, deferred=%s)",
            path.name,
            key,
            prior_filename,
            deferred,
        )

    async def handle(self, ctx: DaemonContext, event: SurfaceEvent) -> None:
        """Run one surface turn, then release the dispatched-set
        slot so a re-drop of the same filename can dispatch again."""
        from .._dispatch import handle_surface

        try:
            await handle_surface(ctx, event)
        finally:
            self._dispatched.discard(event.path.name)

    def archive_unresolved(self, path: pathlib.Path) -> None:
        """Move a surface that closed without a ``resolve_surface``
        call into the dated handled-dir, with an inline trailer
        documenting the auto-archival.

        Lives on the watcher (Phase 6c of plan 01) so the surface
        directory layout stays in one place. Called from
        :func:`alice_speaking._dispatch.handle_surface` when the
        kernel returns without Alice calling ``resolve_surface``.
        """
        today = datetime.date.today().isoformat()
        dest_dir = self._handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + "verdict: (unresolved — agent did not call resolve_surface)\n"
            + "action_taken: auto-archived by daemon\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info("auto-archived unresolved surface: %s", path.name)


def _read_surface_type(path: pathlib.Path) -> Optional[str]:
    """Extract ``surface_type`` from a surface file's YAML frontmatter.

    Returns ``None`` when the file is missing, unreadable, or the
    frontmatter lacks a ``surface_type:`` line. Reads only the first
    ~2 KiB so a malformed body can't stall the watcher.
    """
    head = _read_frontmatter_head(path)
    if head is None:
        return None
    match = _SURFACE_TYPE_RE.search(head)
    if match is None:
        return None
    return match.group(1).strip().strip("\"'")


def _read_violation_count(path: pathlib.Path) -> Optional[int]:
    """Extract ``violation_count`` from a surface file's YAML frontmatter.

    Returns ``None`` when the file is missing/unreadable, the
    frontmatter lacks a ``violation_count:`` line, or the value isn't
    a non-negative integer. Reads only the first ~2 KiB so a malformed
    body can't stall the watcher.
    """
    head = _read_frontmatter_head(path)
    if head is None:
        return None
    match = _VIOLATION_COUNT_RE.search(head)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_frontmatter_head(path: pathlib.Path) -> Optional[str]:
    """Return the first ~2 KiB of a surface file, or None on I/O error.
    Shared by frontmatter parsers so a malformed body can't stall the
    watcher."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(2048)
    except OSError:
        return None


def _dedup_key(path: pathlib.Path) -> Optional[str]:
    """Compute the id+date dedup key for a surface.

    Order of preference for each component:
      * source-id: ``source-id`` frontmatter field, else the filename
        slug after the ``YYYY-MM-DD-HHMMSS-`` prefix.
      * date:      ``date`` frontmatter field, else the ``YYYY-MM-DD``
        prefix of the filename.

    Returns ``None`` when neither path yields both components — those
    surfaces fall through to no-dedup, matching today's behaviour.
    """
    head = _read_frontmatter_head(path)
    source_id: Optional[str] = None
    date: Optional[str] = None
    if head is not None:
        m = _SOURCE_ID_RE.search(head)
        if m is not None:
            source_id = m.group(1).strip().strip("\"'")
        m = _FRONTMATTER_DATE_RE.search(head)
        if m is not None:
            date = m.group(1).strip().strip("\"'")
    if source_id is None or date is None:
        m = _FILENAME_DATE_SLUG_RE.match(path.stem)
        if m is not None:
            if date is None:
                date = m.group(1)
            if source_id is None:
                source_id = m.group(2)
    if not source_id or not date:
        return None
    return f"{source_id}|{date}"
