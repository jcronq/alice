"""Signal slash-command dispatcher (issue #624).

Intercepts Signal inbound messages that start with ``/verb`` at the
ingest layer — BEFORE the normal turn pipeline runs — so commands like
``/interrupt``, ``/pause``, and ``/help`` short-circuit Speaking's
kernel entirely.

Design
------
A single :data:`SLASH_COMMANDS` registry is the source of truth for:

- which verbs exist and what they do (shared with the status-site tab
  and the ``/help`` command's rendering),
- the per-command ACL (Jason gets all; Katie gets a narrow subset),
- the handler function that produces a :class:`Response`.

:func:`handle` is the single entry point. The caller
(:func:`_dispatch._maybe_dispatch_slash`) passes the inbound body,
the resolved principal id, and the daemon context; this module owns:

- the double-slash escape (``//foo`` → literal ``/foo``, run normal turn),
- recognition of the verb set (unknown verbs fall through as literal
  content — caller gets ``None`` and runs the normal turn),
- producing a :class:`Response` whose ``reply_text`` short-circuits the
  turn pipeline, or whose ``rewritten_text`` mutates the inbound and
  runs the normal turn.

ACL rules (issue #624 decisions):

- Jason (``jcronq``) → every verb.
- Katie (``katie``) → ``/ack``, ``/food``, ``/log`` only.
- Everyone else → every verb is denied; the dispatcher returns a short
  "command not available" reply and logs the rejection.

The rules live on each :class:`SlashCommand` as ``acl`` — either the
string ``"all"`` (every allowed principal) or a tuple of principal ids.
Whether a principal is "allowed" at all is a separate gate handled by
the address book; this module trusts the caller to have resolved the
sender to a principal id before invoking :func:`handle`.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

from . import slash_state

if TYPE_CHECKING:
    from .transports.base import DaemonContext


log = logging.getLogger(__name__)


@dataclass
class Response:
    """Outcome of a slash-command invocation.

    One of ``reply_text`` / ``rewritten_text`` is populated at a time:

    - ``reply_text`` set, ``rewritten_text`` None → the caller sends the
      reply text directly on the originating channel and skips the
      normal turn pipeline.
    - ``rewritten_text`` set, ``reply_text`` None → the caller replaces
      the head envelope's body with ``rewritten_text`` and runs the
      normal turn. Used for the ``//foo`` escape and skill-shortcut
      verbs that bypass intent classification.

    Both-None is legal (command recognized, no observable action).
    """

    reply_text: Optional[str] = None
    rewritten_text: Optional[str] = None


Handler = Callable[[str, "DaemonContext", str], Awaitable[Response]]


# Jason's principal id has drifted — the live principals.yaml records
# ``jcronq`` but the slice-1 tests + docs refer to ``jason``. Admit both
# so renames don't break the ACL.
_JASON_ONLY = ("jcronq", "jason")
# Katie gets a narrow subset — ``/ack``, ``/food``, ``/log`` — per the
# issue #624 decision. Jason is always included in Katie's ACL so a
# verb's acl tuple fully enumerates who may run it.
_KATIE_SUBSET = ("jcronq", "jason", "katie")


@dataclass(frozen=True)
class SlashCommand:
    """One entry in the slash-command registry.

    ``verb`` is the lowercase token following the leading ``/``. ``help``
    is a one-line description rendered by ``/help`` and the status-site
    tab. ``acl`` is a tuple of principal ids (case-insensitive) that may
    run the verb. ``category`` is a short tag for status-site grouping
    (``control`` / ``voice`` / ``memory`` / ``dispatch`` / ``skill`` /
    ``meta``).
    """

    verb: str
    category: str
    help: str
    acl: tuple[str, ...] = _JASON_ONLY
    handler: Optional[Handler] = None

    def principal_allowed(self, principal_id: str) -> bool:
        """True when ``principal_id`` may invoke this command."""
        if not principal_id:
            return False
        lowered = principal_id.lower()
        return any(entry.lower() == lowered for entry in self.acl)


# Skill shortcuts bypass intent classification by rewriting the inbound
# to name the skill explicitly, then running the normal turn.
_SKILL_SHORTCUTS: dict[str, str] = {
    "food": "log-meal",
    "workout": "log-workout",
    "weight": "update-weight",
    "log": "log-journal",
}


# ---------------------------------------------------------------------------
# Parser


def _parse(text: str) -> Optional[tuple[str, str]]:
    """Split ``text`` into ``(verb, remainder)`` when it opens with
    ``/verb``.

    Returns ``None`` when the text does not begin with a single leading
    slash (``//foo`` is an escape — handled by the caller, not here —
    and anything with a leading space is treated as content).

    ``verb`` is lowercased so ``/Help``, ``/HELP`` and ``/help`` all
    route the same way.
    """
    if not text or not text.startswith("/"):
        return None
    if text.startswith("//"):
        return None
    body = text[1:]
    m = re.match(r"^(\S+)\s*(.*)$", body, flags=re.DOTALL)
    if m is None:
        return None
    return m.group(1).lower(), m.group(2)


# ---------------------------------------------------------------------------
# Entry point


async def handle(
    text: str,
    principal_id: str,
    ctx: "DaemonContext",
) -> Optional[Response]:
    """Dispatch a candidate slash-command message.

    Returns:

    - ``None`` when ``text`` isn't actually a recognized command (the
      caller runs the normal turn pipeline with the original text).
    - A :class:`Response` for ``//foo`` escape and all recognized
      verbs, including ACL-denied responses.
    """
    # Escape convention: ``//foo`` → literal ``/foo``. Strip one leading
    # slash and ask the caller to run the normal turn with rewritten
    # text.
    if text.startswith("//"):
        return Response(rewritten_text=text[1:])

    parsed = _parse(text)
    if parsed is None:
        return None
    verb, argument = parsed
    cmd = SLASH_COMMANDS_BY_VERB.get(verb)
    if cmd is None:
        # Unknown ``/foo`` — fall through as literal content per the
        # issue's design. The caller's normal turn pipeline will see
        # the original text.
        return None

    if not cmd.principal_allowed(principal_id):
        # Known principal invoking a verb outside their ACL → reply
        # explicitly so the short-circuit is honest; unknown principal
        # (not in any command's ACL) → fall through as literal content,
        # matching the slice-1 behaviour the ACL test asserts.
        if _is_known_slash_principal(principal_id):
            log.warning(
                "slash command /%s denied for principal %r",
                verb,
                principal_id,
            )
            return Response(
                reply_text=(
                    f"/{verb}: not available to you. "
                    "Try /help for the commands you can run."
                )
            )
        log.warning(
            "slash command /%s from unauthorised principal %r; fall-through",
            verb,
            principal_id or "<unknown>",
        )
        return None

    if cmd.handler is None:
        return Response(reply_text=f"/{verb}: no handler wired yet")
    return await cmd.handler(argument, ctx, principal_id)


def _is_known_slash_principal(principal_id: str) -> bool:
    """True when ``principal_id`` appears in ANY command's ACL.

    Used by :func:`handle` to decide whether an ACL-denied verb
    produces a visible "not available" reply or falls through silently
    so the normal pipeline handles the message as literal content.
    """
    if not principal_id:
        return False
    lowered = principal_id.lower()
    for cmd in SLASH_COMMANDS:
        if any(entry.lower() == lowered for entry in cmd.acl):
            return True
    return False


# ---------------------------------------------------------------------------
# /help


async def _handle_help(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Render the registry as a human-readable command list.

    When invoked by a principal with a narrow ACL, only the verbs they
    may actually run are listed so Katie's /help doesn't advertise /go.
    """
    lines = ["Slash commands:"]
    current_category: Optional[str] = None
    for cmd in SLASH_COMMANDS:
        if not cmd.principal_allowed(principal_id):
            continue
        if cmd.category != current_category:
            lines.append("")
            lines.append(f"[{cmd.category}]")
            current_category = cmd.category
        lines.append(f"/{cmd.verb} — {cmd.help}")
    lines.append("")
    lines.append("Escape: //foo sends a literal /foo.")
    return Response(reply_text="\n".join(lines))


# ---------------------------------------------------------------------------
# /status


async def _handle_status(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """One-line readout: current/last turn + queue depth + last outbound.

    Reads from two ground-truth sources, both best-effort:

    - The signal transport's inbox for queue depth (counts queued-but-
      not-yet-dispatched inbound messages across all senders).
    - The event log tail (``cfg.event_log_path``) for last turn + last
      outbound.

    Never raises. On any failure, returns a degraded one-liner so the
    caller still gets a human reply.
    """
    try:
        queue_depth = 0
        signal_tr = getattr(ctx, "signal_transport", None)
        if signal_tr is not None:
            inbox = getattr(signal_tr, "_inbox", None)
            if inbox is not None:
                queue_depth += int(inbox.qsize())
        daemon_queue = getattr(ctx, "_queue", None)
        if daemon_queue is not None:
            queue_depth += int(daemon_queue.qsize())

        current_turn = getattr(ctx, "_current_turn_kind", None) or "idle"

        last_outbound: Optional[str] = None
        last_turn_label: Optional[str] = None
        event_log_path = _resolve_event_log_path(ctx)
        if event_log_path is not None and event_log_path.is_file():
            last_outbound, last_turn_label = _tail_log_snapshot(event_log_path)

        parts = [f"current: {current_turn}", f"queue: {queue_depth}"]
        state_dir = _resolve_state_dir(ctx)
        if slash_state.is_paused(state_dir=state_dir):
            parts.append("paused")
        quiet_deadline = slash_state.quiet_until(state_dir=state_dir)
        if quiet_deadline is not None:
            parts.append(f"quiet_until: {quiet_deadline.isoformat(timespec='minutes')}")
        if last_turn_label:
            parts.append(f"last_turn: {last_turn_label}")
        if last_outbound:
            preview = last_outbound.strip().replace("\n", " ")[:120]
            parts.append(f"last_outbound: {preview}")
        return Response(reply_text=" | ".join(parts))
    except Exception as exc:  # noqa: BLE001
        log.exception("/status failed")
        return Response(reply_text=f"/status: errored ({type(exc).__name__})")


def _resolve_event_log_path(ctx: "DaemonContext") -> Optional[pathlib.Path]:
    cfg = getattr(ctx, "cfg", None)
    if cfg is None:
        return None
    path = getattr(cfg, "event_log_path", None)
    if path is None:
        return None
    return pathlib.Path(path)


def _resolve_state_dir(ctx: "DaemonContext") -> Optional[pathlib.Path]:
    cfg = getattr(ctx, "cfg", None)
    if cfg is None:
        return None
    sd = getattr(cfg, "state_dir", None)
    return pathlib.Path(sd) if sd is not None else None


def _tail_log_snapshot(
    path: pathlib.Path, max_lines: int = 500
) -> tuple[Optional[str], Optional[str]]:
    """Read the tail of ``path`` as JSONL and extract a (last_outbound,
    last_turn_label) pair.

    ``last_outbound`` is the ``text`` of the most recent event whose
    name ends in ``_send``. ``last_turn_label`` is the most recent
    ``*_turn_end`` event's turn kind plus its duration.
    """
    last_outbound: Optional[str] = None
    last_turn_label: Optional[str] = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    tail = lines[-max_lines:]
    for raw in reversed(tail):
        if last_outbound is not None and last_turn_label is not None:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        event = record.get("event")
        if not isinstance(event, str):
            continue
        if last_outbound is None:
            if event.endswith("_send") or event == "signal_send":
                for key in ("text", "preview", "inbound"):
                    value = record.get(key)
                    if isinstance(value, str) and value:
                        last_outbound = value
                        break
        if last_turn_label is None:
            if event.endswith("_turn_end"):
                kind = event[: -len("_turn_end")]
                duration = record.get("duration_ms")
                if isinstance(duration, (int, float)):
                    last_turn_label = f"{kind} ({int(duration)}ms)"
                else:
                    last_turn_label = kind
    return last_outbound, last_turn_label


# ---------------------------------------------------------------------------
# /ack


async def _handle_ack(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Move every pending ``inner/surface/*.md`` into ``.handled/<today>/``.

    "Pending" means flat ``*.md`` under ``inner/surface/`` — the
    SurfaceWatcher's own view. Reading / writing / unlinking are all
    best-effort.
    """
    try:
        surface_dir = _resolve_surface_dir(ctx)
        if surface_dir is None or not surface_dir.is_dir():
            return Response(reply_text="/ack: no surface directory configured")
        handled_dir = surface_dir / ".handled"
        today = datetime.date.today().isoformat()
        dest_dir = handled_dir / today
        acked: list[str] = []
        failed: list[str] = []
        for path in sorted(surface_dir.glob("*.md")):
            if path.name.startswith("."):
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                body = path.read_text()
                # Preserve the legacy "acked by Jason" verdict casing
                # when the principal maps to Jason; everything else is
                # rendered title-case for human readability in the
                # ``.handled/`` trailer.
                _actor = (
                    "Jason"
                    if principal_id.lower() in ("jason", "jcronq")
                    else (principal_id.title() or "unknown")
                )
                trailer = (
                    "\n\n---\n"
                    f"resolved: "
                    f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                    f"verdict: acked by {_actor} via /ack\n"
                    "action_taken: moved to .handled by slash-command dispatcher\n"
                )
                (dest_dir / path.name).write_text(body + trailer)
                path.unlink()
                acked.append(path.name)
            except OSError as exc:
                log.warning("/ack: failed to move %s: %s", path.name, exc)
                failed.append(path.name)
        if not acked and not failed:
            return Response(reply_text="/ack: nothing pending")
        parts = [f"/ack: resolved {len(acked)} surface(s)"]
        if failed:
            parts.append(f"{len(failed)} failed")
        return Response(reply_text=" — ".join(parts))
    except Exception as exc:  # noqa: BLE001
        log.exception("/ack failed")
        return Response(reply_text=f"/ack: errored ({type(exc).__name__})")


def _resolve_surface_dir(ctx: "DaemonContext") -> Optional[pathlib.Path]:
    watcher = getattr(ctx, "_surface_watcher", None)
    if watcher is not None:
        candidate = getattr(watcher, "surface_dir", None)
        if candidate is not None:
            return pathlib.Path(candidate)
    cfg = getattr(ctx, "cfg", None)
    mind_dir = getattr(cfg, "mind_dir", None) if cfg is not None else None
    if mind_dir is None:
        return None
    return pathlib.Path(mind_dir) / "inner" / "surface"


# ---------------------------------------------------------------------------
# /q, /remember, /forget — note writers


async def _handle_q(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Queue the argument as a low-priority manual note."""
    content = argument.strip()
    if not content:
        return Response(reply_text="/q: usage — /q <message>")
    note_path = _write_manual_note(
        ctx, content=content, tag="idea", slug_hint="q-manual", priority="low",
        principal_id=principal_id,
    )
    if note_path is None:
        return Response(reply_text="/q: couldn't write note (no notes dir configured)")
    return Response(reply_text=f"/q: queued as {note_path.name}")


async def _handle_remember(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Drop the argument into ``inner/notes/`` tagged ``idea``."""
    content = argument.strip()
    if not content:
        return Response(reply_text="/remember: usage — /remember <text>")
    note_path = _write_manual_note(
        ctx, content=content, tag="idea", slug_hint="manual", priority=None,
        principal_id=principal_id,
    )
    if note_path is None:
        return Response(
            reply_text="/remember: couldn't write note (no notes dir configured)"
        )
    return Response(reply_text=f"/remember: wrote {note_path.name}")


async def _handle_forget(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Tombstone a memory entry by slug.

    Writes an ``inner/notes/`` tombstone the memory worker's dedupe
    lane picks up — the actual delete happens there, so this is an
    async request (not an immediate vault mutation).
    """
    slug = argument.strip().split(None, 1)[0] if argument.strip() else ""
    if not slug:
        return Response(reply_text="/forget: usage — /forget <slug>")
    body = (
        f"Requesting tombstone of vault note `{slug}` via /forget.\n\n"
        "The memory worker's dedupe lane should move the slug's file\n"
        "to `.archived/<date>/` and emit a conflict-candidate surface\n"
        "if the slug is referenced from other notes.\n"
    )
    note_path = _write_manual_note(
        ctx,
        content=body,
        tag="correction",
        slug_hint=f"forget-{_slugify(slug)}",
        priority=None,
        principal_id=principal_id,
        extra_frontmatter={"target": slug, "op": "tombstone"},
    )
    if note_path is None:
        return Response(
            reply_text="/forget: couldn't write tombstone note (no notes dir)"
        )
    return Response(
        reply_text=f"/forget: tombstone requested for {slug!r} ({note_path.name})"
    )


_SLUG_FORBIDDEN = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    text = text.strip().lower()
    text = _SLUG_FORBIDDEN.sub("-", text).strip("-")
    return (text or "untitled")[:max_len]


def _stamp_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def _derive_title(content: str) -> str:
    for line in content.splitlines():
        cleaned = line.strip().lstrip("#*-").strip()
        if cleaned:
            if len(cleaned) <= 80:
                return cleaned
            cut = cleaned[:80]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            return cut.rstrip(" ,;:.") + "…"
    return "untitled"


def _write_manual_note(
    ctx: "DaemonContext",
    *,
    content: str,
    tag: str,
    slug_hint: str,
    priority: Optional[str],
    principal_id: str = "",
    extra_frontmatter: Optional[dict[str, str]] = None,
) -> Optional[pathlib.Path]:
    """Append a note under ``inner/notes/`` with the shape the memory
    worker + thinking already parse.
    """
    cfg = getattr(ctx, "cfg", None)
    mind_dir = getattr(cfg, "mind_dir", None) if cfg is not None else None
    if mind_dir is None:
        return None
    notes_dir = pathlib.Path(mind_dir) / "inner" / "notes"
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    slug_source = content.split("\n", 1)[0]
    slug = _slugify(slug_source) or _slugify(slug_hint)
    path = notes_dir / f"{_stamp_utc()}-{slug}-{slug_hint}.md"
    created = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    header_lines = [
        "---",
        f'title: "{_derive_title(content).replace(chr(34), chr(39))}"',
        f"tag: {tag}",
        f"created: {created}",
        "note_type: fleeting",
        "source: slash-command",
    ]
    if principal_id:
        header_lines.append(f"author: {principal_id}")
    if priority is not None:
        header_lines.append(f"priority: {priority}")
    if extra_frontmatter:
        for key, value in extra_frontmatter.items():
            header_lines.append(f"{key}: {value}")
    header_lines.append("---")
    header = "\n".join(header_lines) + "\n"
    try:
        path.write_text(header + "\n" + content.rstrip() + "\n")
    except OSError:
        return None
    return path


# ---------------------------------------------------------------------------
# /recall


async def _handle_recall(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Query the cue runner for the top matches and format them as a
    Signal-friendly reply.
    """
    query = argument.strip()
    if not query:
        return Response(reply_text="/recall: usage — /recall <query>")
    try:
        from .retrieval.cue_runner import build_cue_context
    except Exception as exc:  # noqa: BLE001
        log.exception("/recall: cue_runner import failed")
        return Response(
            reply_text=f"/recall: cue runner unavailable ({type(exc).__name__})"
        )
    cfg = getattr(ctx, "cfg", None)
    raw_cfg: dict[str, Any] = {}
    if cfg is not None:
        speaking = getattr(cfg, "speaking", None)
        if isinstance(speaking, dict):
            runner_cfg = speaking.get("cue_runner")
            if isinstance(runner_cfg, dict):
                raw_cfg = dict(runner_cfg)
    raw_cfg["enabled"] = True
    raw_cfg["top_n"] = min(int(raw_cfg.get("top_n", 3)), 3)
    try:
        cue = await build_cue_context(query, raw_cfg)
    except Exception as exc:  # noqa: BLE001
        log.exception("/recall: build_cue_context failed")
        return Response(
            reply_text=f"/recall: cue runner errored ({type(exc).__name__})"
        )
    candidates = list(getattr(cue, "candidates", None) or [])
    if not candidates:
        return Response(reply_text=f"/recall: no vault matches for {query!r}")
    lines = [f"/recall {query!r}:"]
    for cand in candidates[:3]:
        slug = getattr(cand, "slug", None) or getattr(cand, "title", None) or "(no-slug)"
        title = getattr(cand, "title", "") or ""
        if title and title != slug:
            lines.append(f"- {slug} — {title}")
        else:
            lines.append(f"- {slug}")
    return Response(reply_text="\n".join(lines))


# ---------------------------------------------------------------------------
# /pause, /resume, /quiet — daemon flags


async def _handle_pause(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Set the pause sentinel. In-flight work finishes; new turns skip."""
    reason = argument.strip()
    state_dir = _resolve_state_dir(ctx)
    try:
        slash_state.set_paused(
            principal_id=principal_id, reason=reason, state_dir=state_dir
        )
    except OSError as exc:
        return Response(reply_text=f"/pause: failed to write sentinel ({exc})")
    suffix = f" ({reason})" if reason else ""
    return Response(
        reply_text=(
            f"/pause: Speaking paused{suffix}. In-flight work finishes; new "
            "turns skipped. Send /resume to flush."
        )
    )


async def _handle_resume(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Clear the pause sentinel."""
    state_dir = _resolve_state_dir(ctx)
    cleared = slash_state.clear_paused(state_dir=state_dir)
    if not cleared:
        return Response(reply_text="/resume: was not paused")
    return Response(reply_text="/resume: Speaking resumed")


_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs)?\s*$")


def _parse_duration(text: str) -> Optional[datetime.timedelta]:
    """Loose duration parser: ``10m`` / ``2h`` / ``45`` (minutes default)."""
    m = _DURATION_RE.match(text)
    if m is None:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "m").lower()
    if unit.startswith("s"):
        return datetime.timedelta(seconds=value)
    if unit.startswith("h"):
        return datetime.timedelta(hours=value)
    return datetime.timedelta(minutes=value)


async def _handle_quiet(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Mute non-urgent outbound until the quiet deadline elapses.

    Empty argument ⇒ "indefinite" (default 8h cap). Explicit ``off``
    / ``clear`` clears the sentinel. Otherwise parse ``10m`` / ``2h`` /
    ``45`` (bare number is minutes).
    """
    arg = argument.strip().lower()
    state_dir = _resolve_state_dir(ctx)
    if arg in ("off", "clear", "none"):
        cleared = slash_state.clear_quiet(state_dir=state_dir)
        if not cleared:
            return Response(reply_text="/quiet: was not quiet")
        return Response(reply_text="/quiet: cleared")
    if not arg:
        delta = datetime.timedelta(hours=8)
    else:
        parsed = _parse_duration(arg)
        if parsed is None:
            return Response(
                reply_text="/quiet: usage — /quiet <duration e.g. 30m, 2h> or 'off'"
            )
        delta = parsed
    until = datetime.datetime.now().astimezone() + delta
    try:
        slash_state.set_quiet_until(
            until=until, principal_id=principal_id, state_dir=state_dir
        )
    except OSError as exc:
        return Response(reply_text=f"/quiet: failed to write sentinel ({exc})")
    return Response(
        reply_text=f"/quiet: suppressing non-urgent outbound until {until.isoformat(timespec='minutes')}"
    )


# ---------------------------------------------------------------------------
# /interrupt, /cancel


async def _handle_interrupt(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Record an interrupt request and drop pending outbound.

    The sentinel is a ledger the turn-runner polls at tool-call
    boundaries (slice-3 work). For now we also best-effort drop
    pending outbound queued with the signal transport so Jason's
    stop-and-redirect intent isn't swallowed by a half-rendered reply.
    """
    body = argument.strip()
    state_dir = _resolve_state_dir(ctx)
    try:
        slash_state.record_interrupt(
            principal_id=principal_id, message=body, state_dir=state_dir
        )
    except OSError as exc:
        return Response(reply_text=f"/interrupt: failed to write sentinel ({exc})")
    cancelled = await _cancel_pending_outbound(ctx)
    tail = f" Pending outbound cancelled: {cancelled}." if cancelled else ""
    if body:
        return Response(
            reply_text=(
                f"/interrupt: recorded.{tail} Next turn will see your new "
                "message as the primary input."
            )
        )
    return Response(
        reply_text=(
            f"/interrupt: current turn cancellation requested.{tail}"
        )
    )


async def _handle_cancel(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Cancel a specific task (gh issue #N or task slug) or the current turn."""
    target = argument.strip() or "current"
    state_dir = _resolve_state_dir(ctx)
    try:
        slash_state.record_cancel(
            principal_id=principal_id, target=target, state_dir=state_dir
        )
    except OSError as exc:
        return Response(reply_text=f"/cancel: failed to write sentinel ({exc})")
    return Response(reply_text=f"/cancel: recorded request for {target!r}")


async def _cancel_pending_outbound(ctx: "DaemonContext") -> int:
    """Best-effort drain of any queued outbound on the signal transport.

    The transport may not expose a cancel primitive yet — in that case
    we count zero and emit a log line. Returns the number of drops.
    """
    signal_tr = getattr(ctx, "signal_transport", None)
    if signal_tr is None:
        return 0
    cancel = getattr(signal_tr, "cancel_pending", None)
    if cancel is None:
        log.info("/interrupt: signal_transport has no cancel_pending; skipping")
        return 0
    try:
        result = cancel()
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        return int(result or 0)
    except Exception:  # noqa: BLE001
        log.exception("/interrupt: cancel_pending failed")
        return 0


# ---------------------------------------------------------------------------
# /go, /kill, /retry, /dispatch — SM task CLI bridge


_TASK_CLI = "task"


def _task_cli_invoke(args: list[str]) -> tuple[int, str]:
    """Run the ``task`` CLI and return ``(returncode, combined_output)``.

    5-second timeout so a hanging CLI can't wedge the slash-dispatcher.
    """
    try:
        result = subprocess.run(
            [_TASK_CLI, *args],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{_TASK_CLI}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{_TASK_CLI}: timed out after 5s"
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined.strip()


async def _handle_go(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Approve ``sm:design_review`` / unblock ``sm:blocked`` on issue #N."""
    n = argument.strip().lstrip("#")
    if not n:
        return Response(reply_text="/go: usage — /go <issue_number>")
    code, output = _task_cli_invoke(["sm", "select", "--gh", n])
    if code == 0:
        return Response(reply_text=f"/go: #{n} selected. {output[:120]}".rstrip())
    return Response(reply_text=f"/go: task CLI failed ({code}) — {output[:200]}")


async def _handle_kill(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """SIGTERM a named worker. Records a cancel sentinel the dispatcher
    reads at its next tick."""
    target = argument.strip()
    if not target:
        return Response(reply_text="/kill: usage — /kill <issue_number|bg_id>")
    state_dir = _resolve_state_dir(ctx)
    try:
        slash_state.record_cancel(
            principal_id=principal_id, target=target, state_dir=state_dir
        )
    except OSError as exc:
        return Response(reply_text=f"/kill: failed to write sentinel ({exc})")
    return Response(reply_text=f"/kill: cancel recorded for {target!r}")


async def _handle_retry(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Re-dispatch a blocked task from the top of its state."""
    n = argument.strip().lstrip("#")
    if not n:
        return Response(reply_text="/retry: usage — /retry <issue_number>")
    code, output = _task_cli_invoke(["sm", "retry", "--gh", n])
    if code == 0:
        return Response(reply_text=f"/retry: #{n} redispatched. {output[:120]}".rstrip())
    return Response(reply_text=f"/retry: task CLI failed ({code}) — {output[:200]}")


async def _handle_dispatch(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """File a new GH issue + start it at ``sm:selected``."""
    desc = argument.strip()
    if not desc:
        return Response(reply_text="/dispatch: usage — /dispatch <description>")
    code, output = _task_cli_invoke(
        ["create", "--title", desc, "--status", "selected"]
    )
    if code == 0:
        return Response(reply_text=f"/dispatch: created. {output[:160]}".rstrip())
    return Response(reply_text=f"/dispatch: task CLI failed ({code}) — {output[:200]}")


# ---------------------------------------------------------------------------
# Skill shortcuts


async def _handle_skill_shortcut(
    argument: str, ctx: "DaemonContext", principal_id: str
) -> Response:
    """Rewrite the inbound to name the skill explicitly.

    Dispatched from the registry via ``verb``-aware closures (see
    _make_skill_handler). The argument contains both the user's text
    and the verb context via ``argument + '\x00' + verb``? No — the
    registry closure captures the verb directly.
    """
    # Pure stub; the registry wires per-verb closures so this body
    # isn't actually reached in production.
    return Response(reply_text="/skill: unroutable shortcut")


def _make_skill_handler(verb: str, skill_name: str) -> Handler:
    async def _handler(
        argument: str, ctx: "DaemonContext", principal_id: str
    ) -> Response:
        content = argument.strip()
        if not content:
            return Response(reply_text=f"/{verb}: usage — /{verb} <text>")
        return Response(
            rewritten_text=f"Use the {skill_name} skill: {content}"
        )

    return _handler


# ---------------------------------------------------------------------------
# Registry


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    # Control
    SlashCommand(
        verb="interrupt",
        category="control",
        help="<message> — cancel current turn + queued outbound; inject <message> as the new input.",
        handler=_handle_interrupt,
    ),
    SlashCommand(
        verb="cancel",
        category="control",
        help="<task|current> — cancel a specific task or the current turn.",
        handler=_handle_cancel,
    ),
    SlashCommand(
        verb="pause",
        category="control",
        help="[reason] — freeze new Speaking turns and SM transitions.",
        handler=_handle_pause,
    ),
    SlashCommand(
        verb="resume",
        category="control",
        help="unfreeze Speaking and flush any queued input.",
        handler=_handle_resume,
    ),
    SlashCommand(
        verb="status",
        category="control",
        help="one-line readout: current turn + queue depth + last outbound.",
        handler=_handle_status,
    ),
    # Voice
    SlashCommand(
        verb="quiet",
        category="voice",
        help="<duration?|off> — suppress non-urgent outbound until the deadline.",
        handler=_handle_quiet,
    ),
    SlashCommand(
        verb="ack",
        category="voice",
        help="resolve all pending surfaces without triggering a reply.",
        acl=_KATIE_SUBSET,
        handler=_handle_ack,
    ),
    SlashCommand(
        verb="q",
        category="voice",
        help="<msg> — queue as low-priority note for the next idle tick.",
        handler=_handle_q,
    ),
    # Memory
    SlashCommand(
        verb="remember",
        category="memory",
        help="<text> — write a manual idea note to inner/notes/.",
        handler=_handle_remember,
    ),
    SlashCommand(
        verb="forget",
        category="memory",
        help="<slug> — tombstone a memory entry via the memory worker.",
        handler=_handle_forget,
    ),
    SlashCommand(
        verb="recall",
        category="memory",
        help="<query> — top-3 vault matches via the cue runner.",
        handler=_handle_recall,
    ),
    # Dispatch
    SlashCommand(
        verb="go",
        category="dispatch",
        help="<issue_number> — approve or unblock an SM task.",
        handler=_handle_go,
    ),
    SlashCommand(
        verb="kill",
        category="dispatch",
        help="<issue_number|bg_id> — request SIGTERM for a running worker.",
        handler=_handle_kill,
    ),
    SlashCommand(
        verb="retry",
        category="dispatch",
        help="<issue_number> — re-dispatch a blocked task.",
        handler=_handle_retry,
    ),
    SlashCommand(
        verb="dispatch",
        category="dispatch",
        help="<description> — file a new SM task at sm:selected.",
        handler=_handle_dispatch,
    ),
    # Skill shortcuts
    SlashCommand(
        verb="food",
        category="skill",
        help="<text> — run the log-meal skill on <text>.",
        acl=_KATIE_SUBSET,
        handler=_make_skill_handler("food", "log-meal"),
    ),
    SlashCommand(
        verb="workout",
        category="skill",
        help="<text> — run the log-workout skill on <text>.",
        handler=_make_skill_handler("workout", "log-workout"),
    ),
    SlashCommand(
        verb="weight",
        category="skill",
        help="<text> — run the update-weight skill on <text>.",
        handler=_make_skill_handler("weight", "update-weight"),
    ),
    SlashCommand(
        verb="log",
        category="skill",
        help="<text> — run the log-journal skill on <text>.",
        acl=_KATIE_SUBSET,
        handler=_make_skill_handler("log", "log-journal"),
    ),
    # Meta
    SlashCommand(
        verb="help",
        category="meta",
        help="list the slash commands you can run.",
        acl=_KATIE_SUBSET,
        handler=_handle_help,
    ),
)

SLASH_COMMANDS_BY_VERB: dict[str, SlashCommand] = {
    cmd.verb: cmd for cmd in SLASH_COMMANDS
}


# Legacy name retained for existing tests. ``SLICE1_VERBS`` enumerated a
# narrower set; downstream code should migrate to :data:`SLASH_COMMANDS`.
SLICE1_VERBS: tuple[tuple[str, str], ...] = tuple(
    (cmd.verb, cmd.help) for cmd in SLASH_COMMANDS
)


__all__ = [
    "Response",
    "SlashCommand",
    "SLASH_COMMANDS",
    "SLASH_COMMANDS_BY_VERB",
    "SLICE1_VERBS",
    "handle",
]
