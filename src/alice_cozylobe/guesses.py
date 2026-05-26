"""Guess lifecycle for the cozylobe motion-cortex pipeline (Phase 3 of #380).

A *guess* is the lobe's positional inference about who is where, written
as a markdown note in ``~/alice-mind/cozylobe-cortex/guesses/`` every
time the classify pipeline runs. The motion pipeline (Phase 2) emits
inferences via ``inner/notes/`` for thinking to drain; Phase 3 adds the
durable, queryable side of that — a guess record with a lifecycle.

Three confirmation signals govern how a guess matures (design §4.2):

* **Implicit** — N minutes (default 30) pass with no contradicting
  motion event → confidence ``+= 0.1``. Fires at most once per guess.
* **Explicit** — Jason confirms or corrects on Signal. The receive
  channel for those messages lives on the speaking side (out of scope
  here); :class:`GuessLifecycle` exposes
  :meth:`apply_explicit_confirmation` /
  :meth:`apply_explicit_refutation` as the public API so the speaking
  daemon can call them directly.
* **Self-evident** — A subsequent motion event lands in the predicted
  ``next_room_hypothesis`` within 60s → confidence ``+= 0.2`` and
  status flips to ``confirmed``. A subsequent event in a *different*
  room (not the current room, not the predicted next room) refutes
  the guess.

Surface-tier selection lives here too: :func:`surface_threshold` maps
``(confidence, unexpected)`` → ``"silent" | "log" | "actionable"`` per
design §4.5. The motion pipeline calls this to decide whether to write
just the observation note (silent / log) or also drop a surface file
into ``inner/surface/`` for the speaking daemon (actionable).

Naming collides intentionally with :class:`alice_cozylobe.cortex.Guess`,
which is the *frozen read-only* projection of the same on-disk note.
This module's :class:`Guess` is mutable — it carries the lifecycle
state (status flips, confidence bumps, expiry extensions) the read API
doesn't need. Tests + the motion pipeline import from this module;
the read API and the vault walker stay in ``cortex.py``.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, Optional

from . import cortex as cortex_mod
from .motion import MotionEvent, MotionInference


__all__ = [
    "DEFAULT_IMPLICIT_CONFIRMATION_MINUTES",
    "DEFAULT_REFUTED_TTL",
    "DEFAULT_SCRATCH_TTL",
    "DEFAULT_SELF_EVIDENT_WINDOW_S",
    "IMPLICIT_CONFIRMATION_DELTA",
    "SELF_EVIDENT_CONFIRMATION_DELTA",
    "Evidence",
    "Guess",
    "GuessLifecycle",
    "GuessStatus",
    "expire_overdue_guesses",
    "find_recent_guesses",
    "guess_from_inference",
    "load_guess",
    "surface_threshold",
    "write_guess",
]


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants

# Default retention for an ordinary scratch guess. Design §5.1 + §7.5:
# unconfirmed scratch guesses GC after 24h.
DEFAULT_SCRATCH_TTL = timedelta(hours=24)

# Refuted high-confidence guesses retain longer for pattern analysis
# (design §7.5: "Refuted-but-was-confident guesses are the highest-signal
# training data"). Bumped on refutation, not at creation.
DEFAULT_REFUTED_TTL = timedelta(days=30)

# Implicit-confirmation threshold from the Phase 3 spec (issue prompt):
# "After N minutes (default 30 — make configurable) with no contradicting
# event, lift confidence by +0.1." We keep this short (30 min) because
# the lobe wakes every 5 min and the tick is cheap.
DEFAULT_IMPLICIT_CONFIRMATION_MINUTES = 30

# Self-evident window from the Phase 3 spec: "predicted dining → next
# event in dining within 60s → confidence +0.2." Outside this window
# the next event neither confirms nor refutes — the guess's window has
# elapsed and the lifecycle moves on.
DEFAULT_SELF_EVIDENT_WINDOW_S = 60.0

IMPLICIT_CONFIRMATION_DELTA = 0.1
SELF_EVIDENT_CONFIRMATION_DELTA = 0.2

# Surface-threshold gates (design §4.5).
SURFACE_SILENT_MAX = 0.3
SURFACE_ACTIONABLE_MIN = 0.7


# ---------------------------------------------------------------------------
# Status enum + Evidence


class GuessStatus(str, Enum):
    """Lifecycle states for a guess.

    Inherits from ``str`` so the frontmatter round-trip stays trivial —
    ``GuessStatus.PENDING == "pending"`` works directly.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    EXPIRED = "expired"


@dataclass
class Evidence:
    """One piece of evidence supporting (or contradicting) a guess.

    Phase 3 evidence is almost always a motion event; future phases
    will add doorbell, button, etc. Keep the shape narrow until then.
    """

    kind: str
    entity_id: str
    ts: datetime

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "ts": _iso(self.ts),
        }


# ---------------------------------------------------------------------------
# Guess dataclass — mutable, in-memory


@dataclass
class Guess:
    """Mutable in-memory representation of a guess note.

    Round-trips through :func:`write_guess` + :func:`load_guess`. Fields
    map directly to the frontmatter schema from the Phase 3 prompt:

        title, person, location (room), confidence, status, evidence,
        trail_window, expires_at, plus next_room_hypothesis and the
        implicit-confirmation latch.

    Distinct from :class:`alice_cozylobe.cortex.Guess`, which is the
    frozen read-only projection used by the vault walker. The cortex
    side carries everything as-written; this side carries lifecycle
    state and is what the motion pipeline + lifecycle mutate.
    """

    title: str
    person: Optional[str] = None  # bare title, e.g. "Jason" or None for unknown
    room: Optional[str] = None
    confidence: float = 0.0
    status: GuessStatus = GuessStatus.PENDING
    next_room_hypothesis: Optional[str] = None
    next_room_confidence: Optional[float] = None
    evidence: list[Evidence] = field(default_factory=list)
    trail_window: int = 0
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    body: str = ""
    path: Optional[Path] = None
    # Latched true once :meth:`GuessLifecycle.tick` has applied the
    # implicit-confirmation bump. Keeps the bump idempotent across
    # ticks — without it, every 5-min tick would keep adding +0.1.
    implicit_confirmed: bool = False

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc)
        if self.created is None:
            self.created = now
        if self.created.tzinfo is None:
            # Be liberal in what we accept — load_guess may produce a
            # naive datetime on a hand-edited note.
            self.created = self.created.replace(tzinfo=timezone.utc)
        if self.updated is None:
            self.updated = self.created
        if self.updated.tzinfo is None:
            self.updated = self.updated.replace(tzinfo=timezone.utc)
        if self.expires_at is None:
            self.expires_at = self.created + DEFAULT_SCRATCH_TTL
        if self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)

    @property
    def guess_id(self) -> Optional[str]:
        """File stem (``2026-05-26T143015Z-jason-kitchen``) — None until
        the guess has been written and ``path`` is populated."""
        if self.path is None:
            return None
        return self.path.stem


# ---------------------------------------------------------------------------
# IO helpers


def _iso(dt: Optional[datetime]) -> str:
    """Render a datetime in compact ISO-8601 with Z suffix.

    Naive datetimes are treated as UTC (we never write naive). The Z
    form matches what the cortex frontmatter spec in the Phase 3
    prompt shows and what :func:`datetime.fromisoformat` parses back
    in 3.11+.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_filename(dt: datetime) -> str:
    """ISO-ish timestamp safe for use in a filename — drop the colons."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _parse_iso(value: object) -> Optional[datetime]:
    """Best-effort ISO-8601 → datetime. Returns None on garbage."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    # Accept both "Z" and "+00:00" forms.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: Optional[str], fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    s = _SLUG_RE.sub("-", value.lower()).strip("-")
    return s or fallback


def _strip_wikilink(value: object, category: str) -> Optional[str]:
    """Reverse of the ``"[[people/Jason]]"`` frontmatter shape.

    Returns the bare title (``"Jason"``) when the value parses as a
    wikilink, the raw value with the category prefix stripped when it's
    a plain ``"people/Jason"`` string, the lowercase string when it's
    ``"unknown"``, and ``None`` otherwise. Quotes / leading dashes from
    PyYAML rendering are tolerated.
    """
    if value is None:
        return None
    s = str(value).strip().strip("\"'")
    if not s:
        return None
    if s.lower() == "unknown":
        return None
    m = re.match(r"\[\[(?:" + re.escape(category) + r"/)?([^\]]+)\]\]", s)
    if m:
        return m.group(1).strip()
    prefix = f"{category}/"
    if s.startswith(prefix):
        return s[len(prefix):].strip()
    return s


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically via tempfile+rename.

    Same pattern Phase 1's onboarding CLI uses — write to a sibling
    temp file, fsync to the data is on disk, then ``os.replace`` to
    swap. A crash partway leaves either the old file or nothing at
    that path, never a torn write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-", suffix=".md"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Render + parse


def _yaml_scalar(value: str) -> str:
    """Quote a string scalar for the frontmatter when it contains
    YAML-significant characters; otherwise leave unquoted.

    The simple-YAML parser in cortex.py handles both quoted and
    unquoted forms, but PyYAML (which cortex.py prefers when available)
    needs quoting for values containing ``:``, ``#``, leading dashes,
    etc. Wrap in double quotes and escape embedded quotes.
    """
    if any(ch in value for ch in (':', '#', '"', "'", '\n')) or value.startswith(
        ("-", "[", "{", "*", "&", "!", "|", ">", "%", "@", "`")
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _render_guess(guess: Guess) -> str:
    """Render the on-disk markdown for a Guess.

    The frontmatter shape matches the Phase 3 prompt exactly so
    hand-inspection of vault notes is unambiguous. The body section
    carries the prose body (caller-provided) plus a trailing line for
    the typed-weighted next-room edge so the link is canonical-form
    queryable.
    """
    fm_lines: list[str] = ["---"]
    fm_lines.append(f"title: {_yaml_scalar(guess.title)}")
    fm_lines.append("tags: [guess, cozylobe-cortex, scratch]")
    fm_lines.append(f"created: {_iso(guess.created)}")
    fm_lines.append(f"updated: {_iso(guess.updated)}")
    if guess.person:
        fm_lines.append(f'person: "[[people/{guess.person}]]"')
    else:
        fm_lines.append('person: "unknown"')
    if guess.room:
        fm_lines.append(f'location: "[[rooms/{guess.room}]]"')
    else:
        fm_lines.append('location: "unknown"')
    fm_lines.append(f"confidence: {guess.confidence:.3f}")
    fm_lines.append(f"status: {guess.status.value}")
    fm_lines.append(f"trail_window: {guess.trail_window}")
    fm_lines.append(f"expires_at: {_iso(guess.expires_at)}")
    if guess.next_room_hypothesis:
        fm_lines.append(
            f'next_room_hypothesis: "[[rooms/{guess.next_room_hypothesis}]]"'
        )
    if guess.next_room_confidence is not None:
        fm_lines.append(f"next_room_confidence: {guess.next_room_confidence:.3f}")
    if guess.implicit_confirmed:
        fm_lines.append("implicit_confirmed: true")
    fm_lines.append("evidence:")
    for ev in guess.evidence:
        fm_lines.append(f"  - kind: {ev.kind}")
        fm_lines.append(f"    entity_id: {_yaml_scalar(ev.entity_id)}")
        fm_lines.append(f"    ts: {_iso(ev.ts)}")
    fm_lines.append("---")

    body = guess.body.strip()
    if not body:
        body = f"# {guess.title}"
    return "\n".join(fm_lines) + "\n\n" + body + "\n"


def _guess_filename(guess: Guess) -> str:
    """Filename slug: ``<timestamp>-<entity_or_person>-<short_slug>.md``.

    From the Phase 3 spec. timestamp is UTC ISO without colons,
    entity_or_person is the lowercase person slug (``unknown`` when
    we don't have one), short_slug is the room slug. Collisions on
    sub-second writes are vanishingly rare in practice but we tolerate
    them by appending a counter in :func:`write_guess`.
    """
    ts = _iso_filename(guess.created or datetime.now(timezone.utc))
    person = _slug(guess.person, fallback="unknown")
    room = _slug(guess.room, fallback="unknown")
    return f"{ts}-{person}-{room}.md"


def write_guess(vault_root: Path, guess: Guess) -> Path:
    """Write a guess to ``vault_root/guesses/`` atomically.

    Returns the absolute path. If ``guess.path`` is already populated
    (update-in-place — confirmation/refutation lifecycle), reuses the
    existing path so the lifecycle doesn't litter the vault with a new
    file per status change. Otherwise computes the filename via
    :func:`_guess_filename` and writes a fresh file, populating
    ``guess.path`` on the way out so subsequent updates land in the
    same spot.

    Filename collisions (same person/room/second) get a numeric
    suffix; the lifecycle tests rely on this stability.
    """
    guesses_dir = vault_root / "guesses"
    guesses_dir.mkdir(parents=True, exist_ok=True)

    if guess.path is not None:
        target = guess.path
    else:
        target = guesses_dir / _guess_filename(guess)
        # Collision handling: rare but real on tests that batch-write at
        # the same second. Append -01, -02, ... until we find a free slot.
        suffix = 0
        while target.exists():
            suffix += 1
            stem = target.stem.rsplit("-", 1)[0] if target.stem.split("-")[-1].isdigit() else target.stem
            target = guesses_dir / f"{stem}-{suffix:02d}.md"
        guess.path = target

    _atomic_write(target, _render_guess(guess))
    return target


def load_guess(path: Path) -> Guess:
    """Parse a guess note from disk.

    Tolerant of partial frontmatter — missing fields default to safe
    values (status=pending, confidence=0.0, evidence=[]). The created
    field is required for any useful lifecycle work; if it's missing
    we fall back to the file's mtime so the lifecycle doesn't crash
    on a hand-edited note.
    """
    text = path.read_text(encoding="utf-8")
    fm, body = cortex_mod.parse_frontmatter(text)
    title = str(fm.get("title", path.stem))

    person = _strip_wikilink(fm.get("person"), "people")
    room = _strip_wikilink(fm.get("location"), "rooms")
    next_room = _strip_wikilink(fm.get("next_room_hypothesis"), "rooms")

    try:
        confidence = float(fm.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        next_room_conf: Optional[float] = float(fm["next_room_confidence"]) if "next_room_confidence" in fm else None
    except (TypeError, ValueError):
        next_room_conf = None

    status_raw = str(fm.get("status", "pending")).strip().lower()
    try:
        status = GuessStatus(status_raw)
    except ValueError:
        status = GuessStatus.PENDING

    created = _parse_iso(fm.get("created"))
    if created is None:
        try:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            created = datetime.now(timezone.utc)
    updated = _parse_iso(fm.get("updated")) or created
    expires_at = _parse_iso(fm.get("expires_at")) or (created + DEFAULT_SCRATCH_TTL)

    try:
        trail_window = int(fm.get("trail_window", 0))
    except (TypeError, ValueError):
        trail_window = 0

    implicit_raw = fm.get("implicit_confirmed", False)
    implicit_confirmed = (
        bool(implicit_raw)
        if isinstance(implicit_raw, bool)
        else str(implicit_raw).strip().lower() in ("true", "yes", "1")
    )

    evidence: list[Evidence] = []
    evidence_raw = fm.get("evidence") or []
    if isinstance(evidence_raw, list):
        for item in evidence_raw:
            if not isinstance(item, dict):
                continue
            ts = _parse_iso(item.get("ts")) or created
            evidence.append(
                Evidence(
                    kind=str(item.get("kind", "motion")),
                    entity_id=str(item.get("entity_id", "")),
                    ts=ts,
                )
            )

    return Guess(
        title=title,
        person=person,
        room=room,
        confidence=confidence,
        status=status,
        next_room_hypothesis=next_room,
        next_room_confidence=next_room_conf,
        evidence=evidence,
        trail_window=trail_window,
        created=created,
        updated=updated,
        expires_at=expires_at,
        body=body.lstrip("\n"),
        path=path,
        implicit_confirmed=implicit_confirmed,
    )


# ---------------------------------------------------------------------------
# Query + retention


def find_recent_guesses(
    vault_root: Path,
    *,
    person: Optional[str] = None,
    room: Optional[str] = None,
    since: Optional[datetime] = None,
    status: Optional[GuessStatus] = None,
) -> list[Guess]:
    """Return guesses matching the optional filters, oldest-first.

    Used by the lifecycle (recent-pending walks for self-evident
    confirmation/refutation, full-walks for implicit confirmation and
    expiry sweeps) and by tests. Missing vault → empty list. The
    ``since`` filter compares against ``created``.
    """
    guesses_dir = vault_root / "guesses"
    if not guesses_dir.is_dir():
        return []
    out: list[Guess] = []
    for md in sorted(guesses_dir.glob("*.md")):
        try:
            guess = load_guess(md)
        except OSError:
            continue
        if person is not None and (guess.person or "").lower() != person.lower():
            continue
        if room is not None and (guess.room or "").lower() != room.lower():
            continue
        if since is not None and (guess.created is None or guess.created < since):
            continue
        if status is not None and guess.status != status:
            continue
        out.append(guess)
    # Sort oldest-first by created. Filename-sort is usually equivalent
    # but explicit sort survives clock skew + manual file moves.
    out.sort(key=lambda g: g.created or datetime.min.replace(tzinfo=timezone.utc))
    return out


def expire_overdue_guesses(vault_root: Path, now: datetime) -> int:
    """Mark every non-expired guess whose ``expires_at`` < now as expired.

    Returns the count of guesses transitioned. Refuted guesses with the
    extended 30-day TTL participate in the same sweep — once their
    extended deadline passes, they expire just like ordinary scratch.
    The function is idempotent: a re-run with the same ``now`` returns
    zero since the previously-expired guesses are skipped.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    count = 0
    for guess in find_recent_guesses(vault_root):
        if guess.status == GuessStatus.EXPIRED:
            continue
        if guess.expires_at is None:
            continue
        if guess.expires_at < now:
            guess.status = GuessStatus.EXPIRED
            guess.updated = now
            write_guess(vault_root, guess)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Surface threshold


def surface_threshold(
    guess: Guess, *, unexpected: bool = False
) -> Literal["silent", "log", "actionable"]:
    """Map a guess + unexpected-event flag to a surface tier.

    Design §4.5:

    * ``silent`` — confidence < 0.3 OR no actionable inference (no
      room → no actionable handle). Just write the observation note.
    * ``log`` — 0.3 ≤ confidence < 0.7, or anything routine. Note
      goes out; thinking promotes to the daily on drain.
    * ``actionable`` — confidence ≥ 0.7 AND ``unexpected`` (security-
      class, novel pattern). Cozylobe additionally drops a surface
      file into ``inner/surface/`` for the speaking daemon.

    Pure function so callers can dry-run the decision in tests.
    """
    conf = guess.confidence if guess.confidence is not None else 0.0
    if guess.room is None and not guess.next_room_hypothesis:
        # Nothing actionable — no current room AND no next-room edge.
        return "silent"
    if conf < SURFACE_SILENT_MAX:
        return "silent"
    if conf >= SURFACE_ACTIONABLE_MIN and unexpected:
        return "actionable"
    return "log"


# ---------------------------------------------------------------------------
# Factory: build a Guess from a MotionInference + batch


def guess_from_inference(
    inference: MotionInference,
    batch: list[MotionEvent],
    *,
    trail_window: int = 0,
    now: Optional[datetime] = None,
    scratch_ttl: timedelta = DEFAULT_SCRATCH_TTL,
) -> Guess:
    """Construct a :class:`Guess` from one classify result.

    Confidence is the top-level ``inference.confidence`` (room
    confidence). Evidence is one ``motion`` entry per event in the
    batch. The body carries the inference's reasoning text plus, when
    the classifier proposed a next room, a typed-weighted IMPLIES edge
    so the relation is queryable via the same inline-edge syntax used
    everywhere else in the vault.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    confidence = inference.confidence if inference.confidence is not None else 0.0
    person = inference.person_hypothesis
    room = inference.current_room
    next_room = inference.next_room_hypothesis
    next_conf = inference.next_room_confidence

    evidence = [
        Evidence(
            kind="motion",
            entity_id=event.entity_id,
            ts=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
        )
        for event in batch
    ]

    person_disp = person or "unknown"
    room_disp = room or "unknown"
    title = f"{person_disp} in {room_disp} at {moment.strftime('%H:%M')}"

    body_lines = [f"# {title}", ""]
    if inference.reasoning:
        body_lines.append(inference.reasoning)
        body_lines.append("")
    if next_room:
        weight = next_conf if next_conf is not None else 0.5
        body_lines.append(
            f"Next-room hypothesis: (IMPLIES:{weight:.2f})[[rooms/{next_room}]]"
        )
    body = "\n".join(body_lines).rstrip()

    return Guess(
        title=title,
        person=person,
        room=room,
        confidence=confidence,
        next_room_hypothesis=next_room,
        next_room_confidence=next_conf,
        evidence=evidence,
        trail_window=trail_window,
        created=moment,
        updated=moment,
        expires_at=moment + scratch_ttl,
        body=body,
    )


# ---------------------------------------------------------------------------
# Lifecycle


ClockFn = Callable[[], datetime]


class GuessLifecycle:
    """Owns the confirmation/refutation rules for cozylobe-cortex guesses.

    Three public seams:

    * :meth:`process_new_event` — called by :class:`alice_cozylobe.motion.MotionPipeline`
      on every motion event. Walks recent pending guesses, applies
      self-evident confirmation/refutation based on the new event's
      room + the guesses' ``next_room_hypothesis``.
    * :meth:`tick` — called periodically (every 5 min — match the
      lobe wake cadence). Applies implicit confirmation to guesses
      past the configured age threshold.
    * :meth:`apply_explicit_confirmation` /
      :meth:`apply_explicit_refutation` — the speaking daemon calls
      these when Jason confirms or corrects on Signal. The receive
      path lives there; the rule application lives here.

    Stateless across calls — every operation re-reads the relevant
    guesses from disk so a process restart preserves the lifecycle.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        clock: Optional[ClockFn] = None,
        implicit_confirmation_minutes: int = DEFAULT_IMPLICIT_CONFIRMATION_MINUTES,
        self_evident_window_s: float = DEFAULT_SELF_EVIDENT_WINDOW_S,
        refuted_ttl: timedelta = DEFAULT_REFUTED_TTL,
    ) -> None:
        self._vault_root = Path(vault_root)
        self._clock: ClockFn = clock or (lambda: datetime.now(timezone.utc))
        self._implicit_minutes = implicit_confirmation_minutes
        self._self_evident_window_s = self_evident_window_s
        self._refuted_ttl = refuted_ttl

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    def now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    # ------------------------------------------------------------------
    # Self-evident confirmation/refutation

    def process_new_event(self, event: MotionEvent) -> list[Guess]:
        """Apply self-evident confirmation/refutation to recent guesses.

        Walks every pending guess created within the last
        ``self_evident_window_s`` seconds. For each:

        * If the new event lands in the predicted ``next_room_hypothesis``
          → confirm (confidence ``+= 0.2``, status → confirmed).
        * If the new event lands in a *different* room (not the
          predicted next room, not the guess's own current room) →
          refute (status → refuted, expires_at extended by 30 days).
        * Otherwise (same room as the guess, or no next-room prediction
          to evaluate against) → leave pending.

        Returns the list of guesses that were updated.
        """
        now = self.now()
        # Filter wider than the window so the lifecycle can still see
        # guesses whose ``created`` may have slight clock skew. The
        # per-guess age check below tightens to the real window.
        since = now - timedelta(seconds=self._self_evident_window_s * 2)
        candidates = find_recent_guesses(
            self._vault_root, since=since, status=GuessStatus.PENDING
        )
        updated: list[Guess] = []
        if not event.room_id:
            return updated
        for guess in candidates:
            if guess.created is None:
                continue
            age = (now - guess.created).total_seconds()
            if age > self._self_evident_window_s:
                continue
            if guess.next_room_hypothesis is None:
                continue
            event_room = event.room_id
            if event_room == guess.next_room_hypothesis:
                guess.confidence = min(
                    1.0,
                    (guess.confidence or 0.0) + SELF_EVIDENT_CONFIRMATION_DELTA,
                )
                guess.status = GuessStatus.CONFIRMED
                guess.updated = now
                guess.evidence.append(
                    Evidence(
                        kind="self_evident_confirmation",
                        entity_id=event.entity_id,
                        ts=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
                    )
                )
                write_guess(self._vault_root, guess)
                updated.append(guess)
            elif event_room != guess.room:
                guess.status = GuessStatus.REFUTED
                guess.expires_at = now + self._refuted_ttl
                guess.updated = now
                guess.evidence.append(
                    Evidence(
                        kind="self_evident_refutation",
                        entity_id=event.entity_id,
                        ts=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
                    )
                )
                write_guess(self._vault_root, guess)
                updated.append(guess)
            # Same room as the guess: neither confirm nor refute.
        return updated

    # ------------------------------------------------------------------
    # Implicit confirmation

    def tick(self, now: Optional[datetime] = None) -> list[Guess]:
        """Apply implicit confirmation to any pending guess past the
        ``implicit_confirmation_minutes`` threshold.

        Idempotent across ticks: once a guess has had the +0.1 bump
        applied, ``implicit_confirmed`` latches true and subsequent
        ticks skip it. Returns the list of guesses that were updated.
        """
        moment = now or self.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        threshold = timedelta(minutes=self._implicit_minutes)
        updated: list[Guess] = []
        for guess in find_recent_guesses(
            self._vault_root, status=GuessStatus.PENDING
        ):
            if guess.implicit_confirmed:
                continue
            if guess.created is None:
                continue
            if (moment - guess.created) < threshold:
                continue
            guess.confidence = min(
                1.0, (guess.confidence or 0.0) + IMPLICIT_CONFIRMATION_DELTA
            )
            guess.implicit_confirmed = True
            guess.updated = moment
            write_guess(self._vault_root, guess)
            updated.append(guess)
        return updated

    # ------------------------------------------------------------------
    # Explicit confirmation/refutation

    def apply_explicit_confirmation(
        self,
        guess_id: str,
        *,
        person: Optional[str] = None,
        room: Optional[str] = None,
        by: str = "jason",
    ) -> Optional[Guess]:
        """Mark a guess as explicitly confirmed.

        ``guess_id`` is the file stem (without ``.md``). Optional
        overrides for ``person`` / ``room`` let the speaking daemon
        carry through a correction ("no that was Katie") in the same
        call. Returns the updated guess, or None if the id doesn't
        resolve to a file.
        """
        guess = self._load_by_id(guess_id)
        if guess is None:
            return None
        guess.confidence = 1.0
        guess.status = GuessStatus.CONFIRMED
        now = self.now()
        guess.updated = now
        if person is not None:
            guess.person = person or None
        if room is not None:
            guess.room = room or None
        guess.evidence.append(
            Evidence(kind="explicit_confirmation", entity_id=by, ts=now)
        )
        write_guess(self._vault_root, guess)
        return guess

    def apply_explicit_refutation(
        self, guess_id: str, reason: str = "", *, by: str = "jason"
    ) -> Optional[Guess]:
        """Mark a guess as explicitly refuted; extend expiry by 30 days
        so pattern analysis can find it.
        """
        guess = self._load_by_id(guess_id)
        if guess is None:
            return None
        now = self.now()
        guess.status = GuessStatus.REFUTED
        guess.updated = now
        guess.expires_at = now + self._refuted_ttl
        guess.evidence.append(
            Evidence(
                kind="explicit_refutation",
                entity_id=f"{by}:{reason}" if reason else by,
                ts=now,
            )
        )
        write_guess(self._vault_root, guess)
        return guess

    def _load_by_id(self, guess_id: str) -> Optional[Guess]:
        # Strip a stray .md suffix if the caller passed one.
        if guess_id.endswith(".md"):
            guess_id = guess_id[:-3]
        path = self._vault_root / "guesses" / f"{guess_id}.md"
        if not path.is_file():
            return None
        try:
            return load_guess(path)
        except OSError as exc:
            log.warning("guess_lifecycle: failed to load %s: %s", path, exc)
            return None
