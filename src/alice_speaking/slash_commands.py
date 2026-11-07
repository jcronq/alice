"""Signal slash-command dispatcher (issue #624, slice-1).

Intercepts a Jason-only subset of Signal inbound messages that start with
``/verb`` — ``/help``, ``/status``, ``/ack``, ``/q <msg>``, ``/recall
<query>``, ``/remember <text>`` — and produces either a direct reply or a
rewritten inbound for the normal turn pipeline.

Slice-1 scope: no interrupt / cancel / pause / resume / skill shortcuts.
See issue #624 for the deferred slice-2 verbs and the four open scope
questions whose chosen defaults are recorded in the PR body.

Design
------
``handle(text, principal_id, ctx)`` is the single entry point. The caller
gates on ``text.startswith("/")`` + sender identity before invoking; this
module owns:

- the double-slash escape (``//foo`` → literal ``/foo``, run normal turn),
- recognition of the verb set (unknown verbs fall through as literal
  content — caller gets ``None`` and runs the normal turn),
- producing a :class:`Response` whose ``reply_text`` short-circuits the
  turn pipeline, or whose ``rewritten_text`` mutates the inbound and runs
  the normal turn.

``handle`` is ``async`` because ``/recall`` reaches the cue runner, which
is async. All other commands return their :class:`Response` synchronously
and wrap into an awaited future on the way out.

Non-Jason principals never reach ``handle`` in production — the caller in
``_dispatch.handle_signal`` gates on ``principal_id == "jason"`` and logs
a warning without replying for any other sender. The ACL test asserts the
gate works even when a slash-looking message arrives from a non-Jason
address.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .transports.base import DaemonContext


log = logging.getLogger(__name__)


# Slice-1 verb set. Stored as a tuple of (verb, one-line-help) so /help
# renders from the same source as the dispatch table — one source of
# truth for the command list.
SLICE1_VERBS: tuple[tuple[str, str], ...] = (
    ("help", "list available slash commands"),
    ("status", "one-line readout: current/last turn + queue depth + last outbound"),
    ("ack", "resolve all pending surfaces without triggering a reply"),
    ("q", "/q <msg> — queue as low-priority note for the next idle tick"),
    ("recall", "/recall <query> — top-3 vault matches via the cue runner"),
    ("remember", "/remember <text> — write a manual idea note to inner/notes/"),
    ("food", "/food <text> — run the log-meal skill on <text>"),
    ("workout", "/workout <text> — run the log-workout skill on <text>"),
    ("weight", "/weight <text> — run the update-weight skill on <text>"),
    ("log", "/log <text> — run the log-journal skill on <text>"),
)

# Skill shortcuts: verb -> skill name. They bypass intent classification by
# rewriting the inbound to name the skill explicitly, then run the normal turn.
_SKILL_SHORTCUTS: dict[str, str] = {
    "food": "log-meal",
    "workout": "log-workout",
    "weight": "update-weight",
    "log": "log-journal",
}

# Fast lookup for recognition. Values are intentionally unused — the
# handler dispatches by verb inside _dispatch_known_verb.
_KNOWN_VERBS: frozenset[str] = frozenset(v for v, _ in SLICE1_VERBS)


@dataclass
class Response:
    """Outcome of a slash-command invocation.

    One of ``reply_text`` / ``rewritten_text`` is populated at a time:

    - ``reply_text`` set, ``rewritten_text`` None → the caller sends the
      reply text directly on the originating channel and skips the normal
      turn pipeline.
    - ``rewritten_text`` set, ``reply_text`` None → the caller replaces
      the head envelope's body with ``rewritten_text`` and runs the
      normal turn. Used today only for the ``//foo`` escape.

    Both-None is legal (command recognized, no observable action) but no
    slice-1 verb produces it.
    """

    reply_text: Optional[str] = None
    rewritten_text: Optional[str] = None


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
    body = text[1:]  # drop one leading slash
    # Split on first whitespace. If no whitespace, the whole remainder
    # IS the verb, with an empty argument.
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
    """Dispatch a candidate slash-command message for Jason.

    Preconditions (enforced at the caller, not here):

    - ``principal_id == "jason"`` — non-Jason principals should never
      reach this function in slice-1.
    - ``text`` starts with ``/`` — the caller already fast-pathed the
      common case of normal content.

    Returns:

    - ``None`` when ``text`` isn't actually a recognized command (the
      caller runs the normal turn pipeline with the original text).
    - A :class:`Response` for ``//foo`` escape and all recognized
      verbs.
    """
    # Caller is expected to pre-gate, but re-check here so direct calls
    # (tests, future callers) can't accidentally bypass the ACL.
    if principal_id.lower() != "jason":
        log.warning(
            "slash_commands.handle invoked for non-jason principal %r — "
            "rejecting without reply",
            principal_id,
        )
        return None

    # Escape convention: ``//foo`` → literal ``/foo``. We strip exactly
    # one leading slash and ask the caller to run the normal turn with
    # the rewritten text.
    if text.startswith("//"):
        return Response(rewritten_text=text[1:])

    parsed = _parse(text)
    if parsed is None:
        return None
    verb, argument = parsed
    if verb not in _KNOWN_VERBS:
        # Unknown ``/foo`` — fall through as literal content per the
        # issue's design. The caller's normal turn pipeline will see
        # the original text.
        return None

    return await _dispatch_known_verb(verb, argument, ctx)


async def _dispatch_known_verb(
    verb: str, argument: str, ctx: "DaemonContext"
) -> Response:
    """Branch to the per-verb implementation.

    Kept as a flat if/elif ladder so each handler is one grep away;
    there are only six verbs and no inheritance to tangle.
    """
    if verb == "help":
        return _handle_help()
    if verb == "status":
        return _handle_status(ctx)
    if verb == "ack":
        return _handle_ack(ctx)
    if verb == "q":
        return _handle_q(argument, ctx)
    if verb == "recall":
        return await _handle_recall(argument, ctx)
    if verb == "remember":
        return _handle_remember(argument, ctx)
    if verb in _SKILL_SHORTCUTS:
        content = argument.strip()
        if not content:
            return Response(reply_text=f"/{verb}: usage — /{verb} <text>")
        return Response(
            rewritten_text=f"Use the {_SKILL_SHORTCUTS[verb]} skill: {content}"
        )
    # Not reachable — _KNOWN_VERBS gates on the same set — but keep a
    # safe default so a future partial-add can't crash the ingest path.
    return Response(reply_text=f"Unknown slash command: /{verb}")


# ---------------------------------------------------------------------------
# /help


def _handle_help() -> Response:
    lines = ["Slash commands (Jason-only, slice-1):"]
    for verb, doc in SLICE1_VERBS:
        lines.append(f"/{verb} — {doc}")
    lines.append("")
    lines.append("Escape: //foo sends a literal /foo.")
    return Response(reply_text="\n".join(lines))


# ---------------------------------------------------------------------------
# /status


def _handle_status(ctx: "DaemonContext") -> Response:
    """One-line readout: current/last turn + queue depth + last outbound.

    Reads from two ground-truth sources, both best-effort:

    - The signal transport's inbox for queue depth (counts queued-but-
      not-yet-dispatched inbound messages across all senders).
    - The event log tail (``cfg.event_log_path``) for last turn + last
      outbound.

    Never raises. On any failure, returns a degraded one-liner so Jason
    still gets a human reply.
    """
    try:
        # Queue depth across the daemon's dispatcher queue plus the
        # Signal transport's per-transport inbox. One or the other is
        # populated depending on whether the batch coalescer is holding
        # events (slice-2 of plan 01). Both read defensively.
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
        if last_turn_label:
            parts.append(f"last_turn: {last_turn_label}")
        if last_outbound:
            # Cap preview so the Signal reply stays comfortably under
            # 2000 bytes even if the last outbound was a wall of text.
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


def _tail_log_snapshot(
    path: pathlib.Path, max_lines: int = 500
) -> tuple[Optional[str], Optional[str]]:
    """Read the tail of ``path`` as JSONL and extract a (last_outbound,
    last_turn_label) pair.

    ``last_outbound`` is the ``text`` of the most recent event whose
    name ends in ``_send`` (signal_send, discord_send, etc.) — these
    carry outbound-render metadata. ``last_turn_label`` is the most
    recent ``*_turn_end`` event's turn kind ("signal", "cli", …) plus
    its duration, or just the kind if duration is absent.

    ``max_lines`` is cheap: event-log lines are small JSON objects, and
    reading the last few hundred as a block covers the recent history
    we care about. If the file is smaller than ``max_lines``, we read
    everything.
    """
    last_outbound: Optional[str] = None
    last_turn_label: Optional[str] = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    tail = lines[-max_lines:]
    # Walk newest-to-oldest so we stop at the first hit for each field.
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
            # Any transport's send event carries the rendered text.
            if event.endswith("_send") or event == "signal_send":
                # Prefer "text", fall back to "preview" if the schema
                # chose a different field name.
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


def _handle_ack(ctx: "DaemonContext") -> Response:
    """Move every currently-pending ``inner/surface/*.md`` into
    ``.handled/<today>/`` with an ``acked by Jason`` verdict, and reply
    with the count.

    "Pending" means flat ``*.md`` under ``inner/surface/`` — the
    SurfaceWatcher's own view. Files under ``.handled/`` or any other
    subdirectory are ignored. Reading / writing / unlinking are all
    best-effort; a single file that fails to move logs a warning and
    is skipped rather than aborting the whole batch.
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
                trailer = (
                    "\n\n---\n"
                    f"resolved: "
                    f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                    "verdict: acked by Jason via /ack\n"
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
    """Prefer the watcher's own ``surface_dir`` so we never disagree with
    it on where surfaces live. Fall back to ``cfg.mind_dir / inner /
    surface`` when no watcher is wired (tests / partial contexts)."""
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
# /q and /remember — both write to inner/notes/


def _handle_q(argument: str, ctx: "DaemonContext") -> Response:
    """Queue the argument as a low-priority manual note.

    Slice-1 semantics: write ``inner/notes/<stamp>-q-manual.md`` with
    ``priority: low`` + ``tag: idea`` frontmatter. Speaking picks these
    up on the next idle tick via the memory worker's inbox drain; no
    pre-empt of the current turn.

    Empty argument is a parse error — reply with a usage hint rather
    than writing an empty note.
    """
    content = argument.strip()
    if not content:
        return Response(reply_text="/q: usage — /q <message>")
    note_path = _write_manual_note(
        ctx, content=content, tag="idea", slug_hint="q-manual", priority="low"
    )
    if note_path is None:
        return Response(reply_text="/q: couldn't write note (no notes dir configured)")
    return Response(reply_text=f"/q: queued as {note_path.name}")


def _handle_remember(argument: str, ctx: "DaemonContext") -> Response:
    """Drop the argument into ``inner/notes/`` tagged ``idea``.

    No priority annotation — these are standard idea notes the memory
    worker drains on its own cadence.
    """
    content = argument.strip()
    if not content:
        return Response(reply_text="/remember: usage — /remember <text>")
    note_path = _write_manual_note(
        ctx, content=content, tag="idea", slug_hint="manual", priority=None
    )
    if note_path is None:
        return Response(
            reply_text="/remember: couldn't write note (no notes dir configured)"
        )
    return Response(reply_text=f"/remember: wrote {note_path.name}")


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
) -> Optional[pathlib.Path]:
    """Append a note under ``inner/notes/`` with the shape the memory
    worker + thinking already parse.

    Returns the written path, or ``None`` when the notes dir can't be
    located (tests / minimal ctx without ``cfg.mind_dir``).
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
    if priority is not None:
        header_lines.append(f"priority: {priority}")
    header_lines.append("---")
    header = "\n".join(header_lines) + "\n"
    try:
        path.write_text(header + "\n" + content.rstrip() + "\n")
    except OSError:
        return None
    return path


# ---------------------------------------------------------------------------
# /recall


async def _handle_recall(argument: str, ctx: "DaemonContext") -> Response:
    """Query the cue runner for the top matches and format them as a
    Signal-friendly reply.

    Overrides ``enabled=True`` on the cue-runner config regardless of
    the deploy's default, since /recall is an explicit opt-in — Jason
    is asking for vault retrieval right now, not implicit per-turn
    retrieval. Top-N is capped to 3 for a readable Signal message.

    Empty query is a parse error — reply with usage.
    """
    query = argument.strip()
    if not query:
        return Response(reply_text="/recall: usage — /recall <query>")
    # Local import so a missing cue_runner module doesn't break import
    # of this file (and hence the whole signal ingest path).
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
    # Explicit opt-in override: /recall always runs, even when the deploy
    # keeps the per-turn cue runner off. Cap top_n at 3 for Signal.
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


__all__ = ["Response", "SLICE1_VERBS", "handle"]
