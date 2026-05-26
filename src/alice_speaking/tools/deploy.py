"""Self-deploy tools.

Speaking can request her own process restart by writing a sentinel file at
``/state/worker/reload-requested``. An in-worker s6 service
(``alice-reload-watcher``) sees the file via inotify, sleeps a 3s grace
period, then triggers an internal restart loop that reloads the Python
modules without rebuilding the container.

The mechanism is deliberately scoped to "hot reload" — bind-mounted source
code is already present in the container, only the Python interpreter holds
stale imports. Container-level deploys (Dockerfile or dependency changes)
remain Jason's manual ``alice-deploy worker`` flow.

Design: ``cortex-memory/research/2026-05-05-speaking-self-deploy-design.md``.
Implementation gaps fixed per
``cortex-memory/research/2026-05-05-speaking-self-deploy-impl-gaps.md``:
- async tool returning dict[str, Any] per the fs.py pattern
- ``_git_head`` helper defined; ``inner/state/reload-expected-head`` written
  before the sentinel for post-restart verification
- s6 watcher itself enforces a 60s rate limit (separate concern)
- s6 run script preserves ``s6-setuidgid alice`` (separate concern)
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


_SENTINEL_PATH = Path("/state/worker/reload-requested")
_EXPECTED_HEAD_PATH = (
    Path.home() / "alice-mind" / "inner" / "state" / "reload-expected-head"
)
_COZYLOBE_SENTINEL_PATH = Path("/state/worker/cozylobe-reload-requested")
_COZYLOBE_EXPECTED_HEAD_PATH = (
    Path.home() / "alice-mind" / "inner" / "state" / "cozylobe-reload-expected-head"
)
_REPO_PATH = "/home/alice/alice"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _git_head() -> str:
    """Return the short HEAD of the alice repo, or 'unknown' on failure."""
    try:
        return subprocess.check_output(
            ["git", "-C", _REPO_PATH, "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def build(cfg: Config, *, personae: Personae | None = None) -> list[SdkMcpTool[Any]]:
    p = personae or placeholder_personae()
    agent = p.agent.name

    @tool(
        name="request_worker_reload",
        description=(
            f"Request a hot restart of {agent}'s speaking daemon to pick up "
            "code changes from the bind-mounted alice repo. Writes a sentinel "
            "to /state/worker/reload-requested; the alice-reload-watcher s6 "
            "service waits 3s then triggers an internal restart loop. The "
            "Python interpreter recycles, modules reload, the session resumes "
            "from session.json. Total downtime ~5-10s. Use after committing "
            "a code change to master that needs to take effect in the running "
            "process. Send a Signal message announcing the restart BEFORE "
            "calling this tool — the process may die before any later "
            "outbound is delivered. Watcher rate-limits to one reload per 60s "
            "to prevent restart loops on bad code."
        ),
        input_schema={"reason": str},
    )
    async def request_worker_reload(args: dict) -> dict:
        reason = (args.get("reason") or "").strip()
        head = _git_head()
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "type": "hot",
            "reason": reason,
            "ts": ts,
            "git_head": head,
        }

        # Write the expected-head file first so the post-restart daemon can
        # verify it picked up the right commit (read by daemon_ready handler
        # or the morning self-check). Best-effort; do not block on failure.
        try:
            _EXPECTED_HEAD_PATH.parent.mkdir(parents=True, exist_ok=True)
            _EXPECTED_HEAD_PATH.write_text(head)
        except OSError:
            pass

        # Write the sentinel — this is what triggers the watcher.
        try:
            _SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SENTINEL_PATH.write_text(json.dumps(payload))
        except OSError as exc:
            return _err(f"sentinel write failed: {exc}")

        msg = (
            f"Reload requested at git HEAD {head}. "
            "Watcher will restart the speaking process in ~3s; session "
            "resumes warm from session.json. "
        )
        if reason:
            msg += f"Reason: {reason}"
        return _ok(msg.rstrip())

    @tool(
        name="request_cozylobe_reload",
        description=(
            "Request a hot restart of the alice-cozylobe daemon to pick up "
            "code changes from the bind-mounted alice repo. Writes a sentinel "
            "to /state/worker/cozylobe-reload-requested; the "
            "alice-cozylobe-reload-watcher s6 service waits 3s then triggers "
            "the cozylobe restart loop. The Python interpreter recycles and "
            "modules reload; cozylobe re-establishes its cozyhem-engine SSE "
            "connection on startup. Total downtime ~5-10s. Use after committing "
            "a cozylobe code change to master that needs to take effect in the "
            "running process. Unlike request_worker_reload this does NOT kill "
            "the current Signal turn — cozylobe is a separate daemon. Watcher "
            "rate-limits to one reload per 60s to prevent restart loops on bad "
            "code."
        ),
        input_schema={"reason": str},
    )
    async def request_cozylobe_reload(args: dict) -> dict:
        reason = (args.get("reason") or "").strip()
        head = _git_head()
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "type": "hot",
            "reason": reason,
            "ts": ts,
            "git_head": head,
        }

        # Write the expected-head file first so the post-restart cozylobe
        # daemon can verify it picked up the right commit. Best-effort; do
        # not block on failure. Distinct path from speaking's reload sentinel
        # so concurrent reloads don't stomp each other's verification state.
        try:
            _COZYLOBE_EXPECTED_HEAD_PATH.parent.mkdir(parents=True, exist_ok=True)
            _COZYLOBE_EXPECTED_HEAD_PATH.write_text(head)
        except OSError:
            pass

        # Write the sentinel — this is what triggers the watcher.
        try:
            _COZYLOBE_SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            _COZYLOBE_SENTINEL_PATH.write_text(json.dumps(payload))
        except OSError as exc:
            return _err(f"cozylobe sentinel write failed: {exc}")

        msg = (
            f"Cozylobe reload requested at git HEAD {head}. "
            "Watcher will restart alice-cozylobe in ~3s. "
        )
        if reason:
            msg += f"Reason: {reason}"
        return _ok(msg.rstrip())

    return [request_worker_reload, request_cozylobe_reload]


__all__ = ["build", "_git_head"]
