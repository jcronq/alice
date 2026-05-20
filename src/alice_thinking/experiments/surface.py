"""Surface + event-channel + jsonl outputs for completed experiments.

On every experiment completion (success / fail / stub) the runner fires
three side effects:

1. A surface note to ``~/alice-mind/inner/surface/<YYYY-MM-DD>/<HHMMSS>-experiment-complete.md``.
   Thinking's surface watcher will pick this up on her next wake and run a
   surface turn. Frontmatter follows the surface convention (priority,
   context, reply_expected) plus three experiment-specific keys
   (experiment_id, card_path, status).
2. An ``experiment-card`` event on the viewer event channel — emitted via
   the existing :class:`EventLogger`, which the viewer tails from
   ``/state/worker/thinking.log``. Payload mirrors the spec:
   ``experiment_id, card_path, status, dispatched_at, completed_at``.
3. A line appended to ``~/alice-mind/inner/state/experiments.jsonl`` so
   anyone (an aggregator, an audit script, thinking via a Grep) can ask
   "when did experiment X complete?" without parsing the surface dir.

All three are best-effort. A surface-write failure must not block the
event-log or the jsonl append, and vice versa — silent partial-failure is
the price of decoupling the side effects.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from typing import Any, Optional

from core.events import EventEmitter


__all__ = [
    "DEFAULT_SURFACE_DIR",
    "DEFAULT_EXPERIMENTS_JSONL",
    "append_experiments_jsonl",
    "emit_completion_event",
    "write_surface_note",
]


log = logging.getLogger(__name__)


DEFAULT_SURFACE_DIR = pathlib.Path("/home/alice/alice-mind/inner/surface")
DEFAULT_EXPERIMENTS_JSONL = pathlib.Path(
    "/home/alice/alice-mind/inner/state/experiments.jsonl"
)


def _yaml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_surface_note(
    *,
    experiment_id: str,
    card_path: pathlib.Path,
    status: str,
    abstract: str,
    surface_dir: pathlib.Path = DEFAULT_SURFACE_DIR,
    now: Optional[datetime.datetime] = None,
    priority: str = "insight",
    context: str = "",
) -> pathlib.Path:
    """Drop an ``experiment-complete`` surface note. Returns the path
    written so the runner can include it in telemetry.

    Surface frontmatter convention (per inner/surface/.handled/* examples):
    ``priority``, ``context``, ``reply_expected``. Experiment-specific keys
    (``experiment_id``, ``card_path``, ``status``) ride alongside.

    Body is a 3-5 sentence summary lifted from the card abstract. The spec
    says "thinking sees the summary, not the raw transcript" — the abstract
    is the right granularity for that purpose.
    """
    if now is None:
        now = datetime.datetime.now().astimezone()
    date_dir = surface_dir / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%H%M%S')}-experiment-complete-{experiment_id}.md"
    target = date_dir / stem

    abstract_body = (abstract or "").strip() or (
        f"Experiment {experiment_id} completed with status `{status}`. See the "
        f"card for details."
    )
    if not context:
        context = (
            f"Experiment {experiment_id} finished with status `{status}`. "
            f"Card: `{card_path}`."
        )

    frontmatter = "\n".join(
        [
            "---",
            f"priority: {_yaml_str(priority)}",
            f"context: {_yaml_str(context)}",
            "reply_expected: false",
            f"experiment_id: {_yaml_str(experiment_id)}",
            f"card_path: {_yaml_str(str(card_path))}",
            f"status: {_yaml_str(status)}",
            "---",
        ]
    )
    body = "\n".join(
        [
            frontmatter,
            "",
            f"# Experiment {experiment_id} complete ({status})",
            "",
            abstract_body,
            "",
            f"Card: `{card_path}`",
        ]
    )
    target.write_text(body)
    return target


def emit_completion_event(
    emitter: EventEmitter,
    *,
    experiment_id: str,
    card_path: pathlib.Path,
    status: str,
    dispatched_at: datetime.datetime,
    completed_at: datetime.datetime,
    transcript_path: Optional[pathlib.Path] = None,
    duration_seconds: Optional[float] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Emit the ``experiment-card`` event on the viewer event channel.

    The viewer subscribes to ``experiment-card`` events on the thinking
    event log (it already tails ``thinking.log`` via
    :mod:`viewer.sources`). When this event fires the viewer can
    render the card in the canvas pane (see spec §UI canvas presentation).

    ``extra`` lets the runner attach optional debug context (e.g.
    ``failure_reason`` for stub cards). Default emit shape stays stable;
    additional keys are opaque to the viewer.
    """
    payload: dict[str, Any] = {
        "experiment_id": experiment_id,
        "card_path": str(card_path),
        "status": status,
        "dispatched_at": _iso(dispatched_at),
        "completed_at": _iso(completed_at),
    }
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    if duration_seconds is not None:
        payload["duration_seconds"] = float(duration_seconds)
    if extra:
        payload.update(extra)
    try:
        emitter.emit("experiment-card", **payload)
    except Exception:  # noqa: BLE001 — observability must never raise.
        log.exception("failed to emit experiment-card event for %s", experiment_id)


def append_experiments_jsonl(
    *,
    experiment_id: str,
    hypothesis: str,
    dispatched_at: datetime.datetime,
    completed_at: datetime.datetime,
    status: str,
    duration_seconds: Optional[float],
    card_path: pathlib.Path,
    repo_under_test: Optional[str],
    jsonl_path: pathlib.Path = DEFAULT_EXPERIMENTS_JSONL,
) -> None:
    """Append the per-dispatch jsonl record. Audit trail for the
    evaluation-first discipline (one line per dispatch, append-only).

    Schema matches the v2 spec §Logging:
    ``{experiment_id, hypothesis, dispatched_at, completed_at, status,
    duration_seconds, card_path, repo_under_test}``.

    Best-effort: failure is logged but doesn't raise. The card + event +
    surface are the durable surface; the jsonl is the convenience.
    """
    record = {
        "experiment_id": experiment_id,
        "hypothesis": hypothesis,
        "dispatched_at": _iso(dispatched_at),
        "completed_at": _iso(completed_at),
        "status": status,
        "duration_seconds": duration_seconds,
        "card_path": str(card_path),
        "repo_under_test": repo_under_test,
    }
    try:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        log.exception("failed to append experiments.jsonl for %s", experiment_id)


def _iso(dt: datetime.datetime) -> str:
    """Render an aware datetime as ISO-8601, microsecond-stripped."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0).isoformat()
