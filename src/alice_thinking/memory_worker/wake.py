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
4. Run Stage B/C/D + thalamus intake + SM v2 task-index reindex
   (issue #781 — the task index drifts whenever ``task.yaml`` is
   written out-of-band; this wake rebuilds the index on a 2h
   sub-cadence, configurable via
   ``memory_worker.task_reindex_interval_seconds``).
5. Emit a ``memory_worker_heartbeat`` event with the replay report
   so operators can confirm the loop is firing.
6. Exit 0. The s6 supervisor loop sleeps for ``cadence_minutes``
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
import time
from typing import Any, Optional

from core.events import EventLogger

from . import (
    correction_cascade as cascade_mod,
    correction_cascade_auto_propagate as autoprop_mod,
    journal,
    stage_b,
    stage_c,
    stage_d,
    thalamus,
)

# ``task reindex`` is run from this wake on a slower sub-cadence than
# the main 30-min loop (default every 2h). The reindex is cheap (<1s
# for ~900 tasks on local disk per #781 smoke test) but there's no
# gain from running it every tick — the store only drifts when a
# writer bypasses :mod:`alice_forge.task_store`. See issue #781.
TASK_REINDEX_INTERVAL_SECONDS_DEFAULT = 2 * 60 * 60  # 2h
TASK_REINDEX_STAMP_FILENAME = "task-reindex-last-ran"


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

    # Phase 3: register Stage C verifiers BEFORE replay so any
    # pending atomize/archive/dedupe-merge/orphan-link entries from a
    # crashed prior wake can be validated against the live vault
    # state. Without this, replay falls back to the Phase 1 stubs and
    # marks every Stage C op ``skipped``.
    stage_c.register_verifiers(mind)
    # Phase 4: same wiring for Stage D's ``recombination`` op — replay
    # confirms the synthesis file landed AND the audit row exists
    # before marking the journal entry committed.
    stage_d.register_verifiers(mind)

    # Phase 1: journal replay first so any in-flight mutation from a
    # crashed prior wake is verified or skipped before new work runs.
    # Replay is best-effort — a verifier raising is caught inside
    # :func:`journal.replay` and recorded as ``skipped`` rather than
    # crashing the wake.
    report = journal.replay(journal_path)

    # Pre-stage hook: correction-cascade auto-propagation. Fires on
    # EVERY wake (not gated to Stage C) so corrections drain on the
    # 30-min cadence instead of piling up between nightly Stage C
    # runs. Empirical inflow was ~32 corrections/hr; 191 piled up over
    # 18h before manual drain triggered this fix. Per-file vault locks
    # are acquired inside :func:`autoprop_mod.auto_propagate`, so the
    # hook is safe to run alongside Stage B/C/D's own locking.
    # ``trigger="periodic_wake"`` makes the source identifiable in
    # events.jsonl; Stage C's own call site keeps the default
    # ``"stage_c"`` so both call sites stay distinguishable. Skip when
    # ``total_unpropagated == 0`` so quiet cycles emit no event.
    cascade_report = cascade_mod.detect_corrections(mind)
    if cascade_report.total_unpropagated > 0:
        autoprop_mod.auto_propagate(
            mind, cascade_report, trigger="periodic_wake"
        )

    # Phase 2: Stage B inbox drain. Deterministic routing; no LLM
    # calls. Each note's vault write + journal commit must succeed
    # before the note is atomic-renamed into ``.consumed/`` — a
    # writer raising leaves the note in the inbox for the next
    # tick's retry. Stage B operates on ``mind`` directly (the
    # vault root), not on ``--state-dir``.
    #
    # Issue #853: Stage B/C/D + thalamus used to be unguarded, so a
    # raise from any of them killed the whole wake. The DB-refresh
    # phases that used to follow (phases 6/7/8) never ran. Those
    # three are now in the dedicated alice-gh-poll service so a wake
    # death no longer starves the status-site, but the wrap below
    # also keeps the memory-worker's own stats emit intact — easier
    # for operators to correlate a Stage D wedge with a specific
    # stage's error marker.
    try:
        drain = stage_b.run(mind, journal_path=journal_path)
        stage_b_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        drain = stage_b.DrainReport(errors=1)
        stage_b_error = f"{type(exc).__name__}: {exc}"

    # Phase 2b: Thalamus intake drain. Same 30-min cadence as the
    # inner/notes drain above. Applies the coalescing / aggregation
    # rules from the thalamus relay design to
    # ``inner/thalamus/intake/`` and produces filtered/dropped
    # decisions in the sibling directories. No-op when the thalamus
    # directory tree is absent (deployments that haven't rolled out
    # the Phase 1 scaffold yet). See
    # ``research/2026-06-02-thalamus-relay-layer.md`` §2.4 and
    # ``research/2026-08-07-agent-home-automation-integration-architecture.md`` §2.2.
    try:
        thalamus_report = thalamus.run(mind)
        thalamus_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        thalamus_report = thalamus.ThalamusReport(errors=1)
        thalamus_error = f"{type(exc).__name__}: {exc}"

    # Phase 3: Stage C vault grooming. UNION trigger over structural
    # debt + decay + orphan + broken-link signals; incremental cap
    # per category keeps wake budget bounded.
    try:
        groom = stage_c.run(mind, journal_path=journal_path)
        stage_c_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        groom = stage_c.StageCReport()
        stage_c_error = f"{type(exc).__name__}: {exc}"

    # Phase 4: Stage D recombination. Runs after Stage C so any
    # atomize/dedupe that touched a research note this cycle is
    # already reflected in the candidate set's frontmatter timestamps.
    try:
        synth = stage_d.run(mind, journal_path=journal_path)
        stage_d_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        synth = stage_d.StageDReport(
            skipped_reason=f"exception: {type(exc).__name__}: {exc}"
        )
        stage_d_error = f"{type(exc).__name__}: {exc}"

    # Phase 5: SM v2 task-index fsck / reindex (#781). The task store's
    # ``index.jsonl`` drifts whenever a task.yaml is written by a
    # crashed wake, hand-edited, or migrated — #781 observed a ~33%
    # miss rate on a 912-task store. We rebuild the index on a 2h
    # sub-cadence so the drift never grows into a day-scale backlog
    # without demanding operator attention. See
    # :func:`_maybe_reindex_tasks` for the gate and stamp-file logic.
    reindex_result = _maybe_reindex_tasks(
        mind=mind,
        state_dir=pathlib.Path(args.state_dir),
        cfg=cfg,
    )

    # Phases 6/7/8 (state_db derive, sm_seed, gh_poll) were lifted out
    # of this wake and into the dedicated ``alice-gh-poll`` s6 service
    # (issue #853). They had been running at the end of the wake, so a
    # Stage D decay pre-pass that overran the memory-worker's outer
    # ``timeout --kill-after=60 600`` wrapper SIGKILLed the wake before
    # any of them ran — the status-site Overview went blank because
    # ``derive_runs`` had zero rows. See
    # :mod:`alice_state_db.gh_poll_wake` for the new dispatcher.

    # Heartbeat phase: precedence is the heaviest stage that actually
    # ran (D > C > B). ``stage_d.ran`` is True iff a pair was selected
    # this cycle — null-result outcomes still count, because Stage D
    # *did* think this cycle (it just didn't ship).
    if synth.ran:
        heartbeat_phase = "stage_d"
    elif groom.ran:
        heartbeat_phase = "stage_c"
    else:
        heartbeat_phase = "stage_b"

    emitter.emit(
        "memory_worker_heartbeat",
        phase=heartbeat_phase,
        journal_path=str(journal_path),
        cadence_minutes=int(cfg.get("cadence_minutes", DEFAULT_CADENCE_MINUTES)),
        stage_d_model=str(cfg.get("stage_d_model", DEFAULT_STAGE_D_MODEL)),
        stage_d_api_tier_enabled=bool(
            cfg.get("stage_d_api_tier_enabled", DEFAULT_STAGE_D_API_TIER_ENABLED)
        ),
        **report.to_dict(),
        **{f"stage_b_{k}": v for k, v in drain.to_dict().items()},
        stage_b_error=stage_b_error,
        **{f"thalamus_{k}": v for k, v in thalamus_report.to_dict().items()},
        thalamus_error=thalamus_error,
        **{f"stage_c_{k}": v for k, v in groom.to_dict().items()},
        stage_c_ran=bool(groom.ran),
        stage_c_error=stage_c_error,
        **{f"stage_d_{k}": v for k, v in synth.to_dict().items()},
        stage_d_ran=bool(synth.ran),
        stage_d_error=stage_d_error,
        **{f"task_reindex_{k}": v for k, v in reindex_result.items()},
    )

    return 0


def _maybe_reindex_tasks(
    *,
    mind: pathlib.Path,
    state_dir: pathlib.Path,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Run ``task reindex`` on a slow sub-cadence from the memory-worker.

    Gated by a stamp file at ``state_dir / TASK_REINDEX_STAMP_FILENAME``;
    the interval defaults to
    :data:`TASK_REINDEX_INTERVAL_SECONDS_DEFAULT` but can be overridden
    via ``memory_worker.task_reindex_interval_seconds`` in
    ``alice.config.json``. Setting the override to ``0`` or a negative
    number disables the hook entirely (the stamp is never written and
    the status in the heartbeat is ``"disabled"``).

    Returned dict is flattened into the heartbeat event with a
    ``task_reindex_`` prefix so operators can grep for it in
    events.jsonl without decoding a nested blob.
    """
    interval = int(
        cfg.get(
            "task_reindex_interval_seconds",
            TASK_REINDEX_INTERVAL_SECONDS_DEFAULT,
        )
    )
    if interval <= 0:
        return {"ran": False, "status": "disabled"}

    stamp_path = state_dir / TASK_REINDEX_STAMP_FILENAME
    now = time.time()
    if stamp_path.is_file():
        try:
            last = stamp_path.stat().st_mtime
        except OSError:
            last = 0.0
        if now - last < interval:
            return {
                "ran": False,
                "status": "throttled",
                "seconds_until_due": int(interval - (now - last)),
            }

    # Import late so a broken task store can't wedge the memory-worker
    # service startup — a reindex failure is contained to this phase.
    try:
        from alice_forge.task_store import TaskStore

        tasks_dir = mind / "inner" / "tasks"
        store = TaskStore(tasks_dir)
        report = store.reindex()
    except Exception as exc:  # noqa: BLE001 — never kill the wake
        return {
            "ran": True,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # Only bump the stamp on a successful run so a throttled interval
    # doesn't suppress the next retry after a transient failure.
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp_path.touch()
    except OSError:
        # Stamp-file write failure is non-fatal — the reindex succeeded
        # and the next tick will retry the stamp. Worst case we reindex
        # on every wake, which is still ~1s of work.
        pass

    return {
        "ran": True,
        "status": "ok" if not report.errors else "errors",
        "scanned": report.scanned,
        "written": report.written,
        "errors": len(report.errors),
        "added": len(report.added),
        "removed": len(report.removed),
        "changed": len(report.changed),
    }


if __name__ == "__main__":
    sys.exit(main())
