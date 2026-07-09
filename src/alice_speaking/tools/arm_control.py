"""Arm control tools — thin MCP wrappers over the arm-daemon HTTP API.

The daemon runs on the Pi (10.20.30.170) at http://<host>:8091 and owns
``/dev/ttyACM0`` for its lifetime. Speaking talks to it over HTTP+JSON at
the intent level (``goto_pose`` / ``run_choreo`` / ``abort``), which
decouples the motion cadence from Speaking's turn cadence and eliminates
the stutter that comes from turn-gated joint commands.

Design:
  ~/alice-mind/cortex-memory/research/2026-07-09-arm-control-loop-architecture.md

Operational lessons (why ``use_degrees=False``, why we need stall
detection, etc.) live in the ``2026-07-09-alice-arm-first-motion``
note. The daemon enforces those constraints; Speaking just calls the
intent API.

Configuration:
  ``ALICE_ARM_DAEMON_URL`` — override the daemon base URL. Defaults to
  ``http://10.20.30.170:8091``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from claude_agent_sdk import SdkMcpTool, tool

from core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


log = logging.getLogger(__name__)


DEFAULT_ARM_DAEMON_URL = "http://10.20.30.170:8091"

# HTTP timeouts:
#  - state / abort are cheap (< 100 ms round trip)
#  - goto_pose / run_choreo can block for up to ~20 s while the ease-to
#    loop runs. The wave choreo is ~6 s of motion, but the safety envelope
#    (stall detect + backoff) can extend that if a joint pins. 30 s gives
#    us headroom without silently hanging Speaking forever.
_FAST_TIMEOUT = 5.0
_MOTION_TIMEOUT = 30.0


def _base_url() -> str:
    """Read the daemon URL from env at call time so tests can override it."""
    return os.environ.get("ALICE_ARM_DAEMON_URL", DEFAULT_ARM_DAEMON_URL).rstrip("/")


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _format_result(payload: Any) -> str:
    """Emit a compact JSON string so downstream can parse or read directly."""
    try:
        return json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError):
        return str(payload)


async def _get_json(path: str, *, timeout: float = _FAST_TIMEOUT) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def _post_json(
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = _MOTION_TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    """POST and return (status_code, parsed body).

    The daemon returns 409 with a body that describes stalled joints when a
    motion doesn't fully converge — that's not a transport error, so we
    surface the body rather than raising. Genuine 4xx/5xx (bad JSON,
    missing endpoint, server crash) still bubble up as HTTPStatusError.
    """
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body)
    # 409 = "motion completed but joints stalled" — that's a real result,
    # not a transport failure. Anything else non-2xx we let raise.
    if resp.status_code == 409:
        return resp.status_code, resp.json()
    resp.raise_for_status()
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Handler bodies — pulled out of build() so tests can invoke them without
# the SdkMcpTool decorator machinery in the way.
# ---------------------------------------------------------------------------


async def _handle_arm_state(_args: dict) -> dict[str, Any]:
    try:
        data = await _get_json("/state")
    except httpx.HTTPError as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    return _ok(_format_result(data))


async def _handle_arm_goto(args: dict) -> dict[str, Any]:
    pose = args.get("pose")
    if pose is None:
        return _err("missing 'pose' argument")
    # Accept a bare pose name (str) or a joint dict. Both are valid on
    # the daemon side — it dispatches on type.
    if not isinstance(pose, (str, dict)):
        return _err("'pose' must be a string (skill name) or dict of joint->position")
    if isinstance(pose, str) and not pose.strip():
        return _err("'pose' string cannot be empty")
    body = {"pose": pose}
    try:
        _status, data = await _post_json("/goto_pose", body)
    except httpx.HTTPError as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    # If joints stalled the payload says so — surface it as ok text so the
    # agent can decide what to do next. We don't treat stall as a tool
    # error because the daemon still returned a valid result.
    return _ok(_format_result(data))


async def _handle_arm_choreo(args: dict) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _err("missing or empty 'name' argument")
    body = {"name": name.strip()}
    try:
        _status, data = await _post_json("/run_choreo", body)
    except httpx.HTTPError as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    return _ok(_format_result(data))


async def _handle_arm_abort(_args: dict) -> dict[str, Any]:
    try:
        _status, data = await _post_json("/abort", {}, timeout=_FAST_TIMEOUT)
    except httpx.HTTPError as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    return _ok(_format_result(data))


def build(
    cfg: Optional[Config] = None,
    *,
    personae: Optional[Personae] = None,
) -> list[SdkMcpTool[Any]]:
    """Build the arm-control tool list.

    ``cfg`` and ``personae`` are accepted for parity with the other tool
    builders even though arm-control doesn't need any per-hemisphere
    configuration today — the daemon URL is env-driven so tests can
    monkeypatch it without touching Config.
    """
    _ = cfg  # currently unused — parity with sibling builders
    _ = personae or placeholder_personae()

    _GOTO_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pose": {
                "description": (
                    "Either a named pose ('rest' is always available; other "
                    "skill names double as named poses) or an explicit joint "
                    "dict of joint_name -> position in RANGE_M100_100 space "
                    "(-100..+100). Joint names: shoulder_pan, shoulder_lift, "
                    "elbow_flex, wrist_flex, wrist_roll, gripper."
                ),
                # Any type — the handler validates.
            },
        },
        "required": ["pose"],
    }

    _CHOREO_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Named choreography to run. 'wave' is a ~6s friendly "
                    "hello; 'rest' eases to the safe rest pose. GET /skills "
                    "on the daemon lists what's available."
                ),
            },
        },
        "required": ["name"],
    }

    @tool(
        name="arm_state",
        description=(
            "Read the arm's current joint positions and connection status "
            "from the arm-daemon (Pi @ 10.20.30.170:8091 by default; "
            "override via ALICE_ARM_DAEMON_URL). Returns a JSON string with "
            "keys 'joints', 'is_connected', 'timestamp'. Cheap — safe to "
            "call while motion is in flight."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def arm_state(args: dict) -> dict:  # noqa: D401 — MCP handler
        return await _handle_arm_state(args)

    @tool(
        name="arm_goto",
        description=(
            "Move the arm to a target pose. `pose` is either a named pose "
            "string (e.g. 'rest') or a dict of joint_name -> position in "
            "-100..+100 space. Blocks until the ease-to loop settles or a "
            "joint stalls (up to ~30s). Response is a JSON string with "
            "'ok', 'final' (settled joint positions), and 'stalled' (list "
            "of joint names that hit their soft limit)."
        ),
        input_schema=_GOTO_SCHEMA,
    )
    async def arm_goto(args: dict) -> dict:
        return await _handle_arm_goto(args)

    @tool(
        name="arm_choreo",
        description=(
            "Run a named choreography (e.g. 'wave', 'rest'). Blocks until "
            "the sequence finishes or aborts. Response is a JSON string "
            "with 'ok', 'final', 'stalled', and 'choreo'. Use `arm_abort` "
            "to cancel a running choreography from another turn."
        ),
        input_schema=_CHOREO_SCHEMA,
    )
    async def arm_choreo(args: dict) -> dict:
        return await _handle_arm_choreo(args)

    @tool(
        name="arm_abort",
        description=(
            "Cancel any in-progress motion. The daemon's motion loop checks "
            "the abort flag between steps and holds the current position. "
            "Safe to call even if no motion is running."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def arm_abort(args: dict) -> dict:
        return await _handle_arm_abort(args)

    return [arm_state, arm_goto, arm_choreo, arm_abort]


__all__ = [
    "build",
    "_handle_arm_state",
    "_handle_arm_goto",
    "_handle_arm_choreo",
    "_handle_arm_abort",
    "_base_url",
    "DEFAULT_ARM_DAEMON_URL",
]
