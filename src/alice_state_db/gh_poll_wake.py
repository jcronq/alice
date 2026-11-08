"""One-tick entry point for the dedicated ``alice-gh-poll`` s6 service.

Issue #853 decouples GitHub substrate refresh from the vault-grooming
memory-worker wake. Before this module existed, three phases ran at the
end of ``alice_thinking.memory_worker.wake.main()``:

    phase 6: alice_state_db.derive.derive_tasks_workers_events(mind)
    phase 7: alice_state_db.sm_seed.derive_sm_seed(mind)
    phase 8: alice_state_db.gh_poll.derive_gh_poll(mind, repos=…)

…but Stage D's decay pre-pass can exceed the memory-worker's outer
``timeout`` wrapper on large vaults, so those three phases never ran.
The status-site Overview went blank because ``derive_runs`` had zero
rows despite the DB existing.

This module runs those three in order, under a short cadence
(default 90 s, floor 60 s) from a dedicated ``alice-gh-poll`` s6
service. It is intentionally tiny: wake takes a ``--mind`` /
``--log`` / ``--echo`` triple matching ``alice_thinking.memory_worker.wake``,
touches a liveness file, and emits one ``gh_poll_tick`` event with
flattened reports. Stage B/C/D stays in the memory-worker tick.

Config overrides come from ``gh_poll.*`` in ``alice.config.json``:

* ``gh_poll.enabled`` (default ``true``). Falls back to
  ``memory_worker.gh_poll_enabled`` for back-compat with operators
  who already pinned the old wake-based path to a no-op.
* ``gh_poll.poll_interval_seconds`` is read by the shell supervisor,
  not here — kept out of this module so a config edit doesn't force
  a Python reload.
* ``gh_poll.repos`` and (for back-compat) ``memory_worker.gh_poll_repos``
  — list of ``owner/repo`` strings. Falls through to
  :data:`alice_state_db.gh_poll.DEFAULT_REPOS`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from alice_state_db import derive as _derive_mod
from core.events import EventLogger

DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_LOG = pathlib.Path("/state/worker/gh-poll.log")

#: Liveness file the container HEALTHCHECK probes. Same pattern as
#: :data:`alice_thinking.memory_worker.wake.MEMORY_WORKER_LIVENESS_PATH`:
#: touched at the start of each tick, BEFORE any config / vault read,
#: so a wake stuck in the first poll shows up as a stuck mtime in the
#: supervisor's staleness window.
GH_POLL_LIVENESS_PATH = pathlib.Path("/state/worker/gh-poll-alive")


def _touch_liveness(path: pathlib.Path) -> None:
    """Touch the liveness file. Swallows FileNotFoundError only.

    Other OSErrors (PermissionError, read-only FS, etc.) are NOT
    swallowed — those are real failures we want surfaced.
    """
    try:
        path.touch()
    except FileNotFoundError:
        pass


def _load_config(mind: pathlib.Path) -> dict[str, Any]:
    """Pull the ``gh_poll`` and ``memory_worker`` blocks from config.

    Returns a dict with two sub-dicts (``gh_poll`` and ``memory_worker``)
    so callers can read either and fall back cleanly. Missing /
    unreadable config returns the empty-sub-dict shape so callers don't
    have to re-check each key.
    """
    out = {"gh_poll": {}, "memory_worker": {}}
    cfg_path = mind / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return out
    try:
        blob = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(blob, dict):
        return out
    gh = blob.get("gh_poll")
    if isinstance(gh, dict):
        out["gh_poll"] = gh
    mw = blob.get("memory_worker")
    if isinstance(mw, dict):
        out["memory_worker"] = mw
    return out


def _resolve_enabled(cfg: dict[str, dict[str, Any]]) -> bool:
    """``gh_poll.enabled`` wins; fall back to ``memory_worker.gh_poll_enabled``.

    Default when neither is set: ``True``. An operator who already
    pinned the old wake-based poll to a no-op via
    ``memory_worker.gh_poll_enabled = false`` keeps that semantic
    without having to migrate their config.
    """
    gh_cfg = cfg.get("gh_poll") or {}
    if "enabled" in gh_cfg:
        return bool(gh_cfg.get("enabled"))
    mw_cfg = cfg.get("memory_worker") or {}
    if "gh_poll_enabled" in mw_cfg:
        return bool(mw_cfg.get("gh_poll_enabled"))
    return True


def _resolve_repos(cfg: dict[str, dict[str, Any]]) -> list[str] | None:
    """Pick the configured repo list, falling back to
    :data:`alice_state_db.gh_poll.DEFAULT_REPOS` on absence.
    """
    gh_cfg = cfg.get("gh_poll") or {}
    v = gh_cfg.get("repos")
    if isinstance(v, list) and v:
        return [str(r) for r in v if isinstance(r, str)]
    mw_cfg = cfg.get("memory_worker") or {}
    v = mw_cfg.get("gh_poll_repos")
    if isinstance(v, list) and v:
        return [str(r) for r in v if isinstance(r, str)]
    return None


def main(argv: list[str] | None = None) -> int:
    """One cadence tick of the dedicated gh-poll service.

    Runs, in order:

    1. ``derive_tasks_workers_events`` — walks the on-disk task /
       worker / events trees and upserts into ``alice-state.db``.
    2. ``derive_sm_seed`` — folds the GH mirror + task store into the
       SM v2 substrate tables.
    3. ``derive_gh_poll`` — polls GitHub for live issue / PR state.

    Each is wrapped in a telemetry-only try/except: a raise in one
    doesn't starve the others (the shape the ``wake.py`` phases 6/7/8
    used before this split-out). Returns 0 unconditionally so the s6
    supervisor keeps looping.
    """
    _touch_liveness(GH_POLL_LIVENESS_PATH)

    parser = argparse.ArgumentParser(
        description=(
            "One cadence tick of the dedicated GitHub-poll service "
            "(derive_tasks_workers_events + derive_sm_seed + "
            "derive_gh_poll)."
        )
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="event log path")
    parser.add_argument(
        "--echo", action="store_true", help="also echo events to stderr"
    )
    args = parser.parse_args(argv)

    mind = pathlib.Path(args.mind)
    cfg = _load_config(mind)

    # Disabled-in-config short-circuit. No event, no work.
    if not _resolve_enabled(cfg):
        return 0

    emitter = EventLogger(pathlib.Path(args.log), echo=args.echo)

    # Phase 1: derive tasks / workers / events into alice-state.db.
    # Lifted verbatim from wake.py lines 265-272.
    try:
        state_db_report = _derive_mod.derive_tasks_workers_events(mind)
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        state_db_report = _derive_mod.DeriveReport(
            errors=[{"kind": "derive", "path": str(mind), "reason": str(exc)}]
        )

    # Phase 2: SM v2 substrate seed. Lifted from wake.py lines 281-290.
    try:
        from alice_state_db import sm_seed as _sm_seed_mod

        sm_seed_report = _sm_seed_mod.derive_sm_seed(mind)
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        from alice_state_db.sm_seed import SmSeedReport as _SmSeedReport

        sm_seed_report = _SmSeedReport(
            errors=[{"kind": "sm_seed", "path": str(mind), "reason": str(exc)}]
        )

    # Phase 3: live GitHub poll. Lifted from wake.py lines 301-325 but
    # the ``enabled`` gate sits on the outer _resolve_enabled check
    # instead of a memory_worker.gh_poll_enabled sub-flag.
    configured_repos = _resolve_repos(cfg)
    try:
        from alice_state_db import gh_poll as _gh_poll_mod

        gh_poll_report = _gh_poll_mod.derive_gh_poll(
            mind, repos=configured_repos
        )
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        from alice_state_db.gh_poll import PollReport as _PollReport

        gh_poll_report = _PollReport(
            status="error",
            errors=[
                {
                    "kind": "gh_poll",
                    "path": str(mind),
                    "reason": str(exc),
                }
            ],
        )

    emitter.emit(
        "gh_poll_tick",
        **{f"state_db_{k}": v for k, v in state_db_report.to_dict().items()},
        **{f"sm_seed_{k}": v for k, v in sm_seed_report.to_dict().items()},
        **{f"gh_poll_{k}": v for k, v in gh_poll_report.to_dict().items()},
    )

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
