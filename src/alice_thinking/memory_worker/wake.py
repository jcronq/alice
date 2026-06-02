"""Memory worker — one cadence tick.

Mirrors the structure of :mod:`alice_thinking.wake` (the generative
hemisphere) so operators reading either entry point see the same
shape: argparse, config overrides, :class:`EventLogger`, one body
of work, structured exit code. Phase 1 only wires the bookkeeping
loop — Stage B/C/D bodies land in phases 2–4.

What this entry point does today:

1. Touch the liveness probe so the container HEALTHCHECK sees a
   fresh mtime (same pattern as thinking's wake — see
   ``sandbox/Dockerfile`` HEALTHCHECK comment).
2. Load ``memory_worker.*`` overrides from ``alice.config.json``.
3. Replay the write-ahead journal so any in-flight mutation from a
   crashed prior wake is verified or skipped before new work runs.
4. Emit a ``memory_worker_heartbeat`` event with the replay report
   so operators can confirm the loop is firing.
5. Exit 0. The s6 supervisor loop sleeps for ``cadence_minutes``
   and fires us again.

The B/C/D stage dispatch lives between steps 3 and 4 once those
phases ship. The scaffold deliberately exits cleanly on the
heartbeat path so the s6 service can be enabled in production
ahead of the real stages — it'll write journal-replay telemetry
without touching the vault.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from core.events import EventLogger

from . import journal


DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_LOG = pathlib.Path("/state/worker/memory-worker.log")
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_CADENCE_MINUTES = 30
DEFAULT_JOURNAL_PATH = pathlib.Path(
    "/home/alice/alice-mind/inner/state/memory-worker-journal.jsonl"
)
DEFAULT_STAGE_D_MODEL = "local"
DEFAULT_STAGE_D_API_TIER_ENABLED = False


#: Liveness file the container HEALTHCHECK probes for memory-worker
#: wedges. Same pattern as
#: :data:`alice_thinking.wake.THINKING_LIVENESS_PATH`: touched at
#: the start of each tick, BEFORE any vault reads, so a wake stuck
#: in journal replay shows up as a stuck mtime in the supervisor's
#: staleness window. The HEALTHCHECK wiring lands when the
#: memory-worker service is added to ``sandbox/Dockerfile``'s probe
#: in a follow-up; for now the file is written so the wiring is a
#: pure additive change in that PR.
MEMORY_WORKER_LIVENESS_PATH = pathlib.Path("/state/worker/memory-worker-alive")


def _touch_liveness(path: pathlib.Path) -> None:
    """Touch the liveness file. Swallows FileNotFoundError only.

    Extracted as a function so the unit test can pass a tmp_path
    override. Other OSErrors (PermissionError, read-only FS, etc.)
    are NOT swallowed — those are real failures we want surfaced.
    """
    try:
        path.touch()
    except FileNotFoundError:
        pass


def _load_config(mind: pathlib.Path) -> dict[str, Any]:
    """Pull ``memory_worker.*`` from ``alice.config.json``.

    Returns ``{}`` (so callers get module defaults) when the file
    is missing, unreadable, or doesn't contain a
    ``memory_worker`` block.
    """
    cfg_path = mind / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return {}
    try:
        blob = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    section = (blob or {}).get("memory_worker") or {}
    return section if isinstance(section, dict) else {}


def _resolve_journal_path(cfg: dict[str, Any]) -> pathlib.Path:
    """Resolve the journal path, expanding ``~`` if present.

    Config-provided strings are user-edited and routinely contain a
    leading ``~`` — expand it here rather than at every call site.
    """
    raw = cfg.get("journal_path") or str(DEFAULT_JOURNAL_PATH)
    return pathlib.Path(str(raw)).expanduser()


def main() -> int:
    # Liveness heartbeat — fires BEFORE argparse / config / replay.
    # Same rationale as :func:`alice_thinking.wake.main`: a wake
    # that dies before this line is caught by the HEALTHCHECK
    # staleness window; a wake that hangs in replay later gets one
    # more chance on the next cadence tick.
    _touch_liveness(MEMORY_WORKER_LIVENESS_PATH)

    parser = argparse.ArgumentParser(
        description="One memory-worker cadence tick (Phase 1 scaffold)."
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="event log path")
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Worker state dir (must be writable).",
    )
    parser.add_argument(
        "--journal",
        default=None,
        help=(
            "Override the journal path. Defaults to "
            "memory_worker.journal_path in alice.config.json, "
            "falling back to ~/alice-mind/inner/state/"
            "memory-worker-journal.jsonl."
        ),
    )
    parser.add_argument(
        "--echo", action="store_true", help="also echo events to stderr"
    )
    args = parser.parse_args()

    mind = pathlib.Path(args.mind)
    cfg = _load_config(mind)

    # Disabled-in-config short-circuit. Same shape as the
    # ``github_watcher.enabled`` gate Speaking honors — operators
    # can pin the service to a no-op without removing the s6 unit.
    if cfg.get("enabled", True) is False:
        # No event, no work — let the supervisor's next tick decide
        # whether config has changed.
        return 0

    journal_path = (
        pathlib.Path(args.journal).expanduser()
        if args.journal
        else _resolve_journal_path(cfg)
    )

    emitter = EventLogger(pathlib.Path(args.log), echo=args.echo)

    # Phase 1: journal replay only. Phase 2+ inserts B/C/D dispatch
    # between this and the heartbeat emit. The replay is best-effort
    # — a verifier raising is caught inside :func:`journal.replay`
    # and recorded as ``skipped`` rather than crashing the wake.
    report = journal.replay(journal_path)

    emitter.emit(
        "memory_worker_heartbeat",
        phase="scaffold",
        journal_path=str(journal_path),
        cadence_minutes=int(cfg.get("cadence_minutes", DEFAULT_CADENCE_MINUTES)),
        stage_d_model=str(cfg.get("stage_d_model", DEFAULT_STAGE_D_MODEL)),
        stage_d_api_tier_enabled=bool(
            cfg.get("stage_d_api_tier_enabled", DEFAULT_STAGE_D_API_TIER_ENABLED)
        ),
        **report.to_dict(),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
