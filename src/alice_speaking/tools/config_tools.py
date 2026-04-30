"""Config tools — Alice can read and self-tune her runtime config.

Edits are additive (deep-merge) so she can tweak one knob without clobbering
the rest. Writes are atomic via tempfile + replace.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build(
    cfg: Config, *, personae: Personae | None = None
) -> list[SdkMcpTool[Any]]:
    p = personae or placeholder_personae()
    agent = p.agent.name
    config_path = cfg.mind_dir / "config" / "alice.config.json"

    @tool(
        name="read_config",
        description=(
            f"Return the current contents of alice.config.json ({agent}'s "
            f"runtime config — model per role, thinking cadence, quiet hours, "
            f"tool allowlists, etc.). Empty object if the file doesn't exist."
        ),
        input_schema={},
    )
    async def read_config(args: dict) -> dict:
        if not config_path.is_file():
            return _ok("{}")
        return _ok(config_path.read_text())

    @tool(
        name="write_config",
        description=(
            f"Deep-merge a JSON patch into alice.config.json. Only changed keys "
            f"are overwritten; everything else is preserved. Pass the patch as "
            f"a JSON string in `patch`. Changes take effect on {agent}'s next turn "
            f"for hot-reloadable fields (model, quiet_hours, allowed_tools); "
            f"other fields may require a daemon restart."
        ),
        input_schema={"patch": str, "reason": str},
    )
    async def write_config(args: dict) -> dict:
        raw = (args.get("patch") or "").strip()
        if not raw:
            return _err("patch required (JSON string)")
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _err(f"patch is not valid JSON: {exc}")
        if not isinstance(patch, dict):
            return _err("patch must be a JSON object")

        current: dict[str, Any] = {}
        if config_path.is_file():
            try:
                current = json.loads(config_path.read_text())
            except json.JSONDecodeError as exc:
                return _err(f"existing config is corrupt: {exc}")

        merged = _deep_merge(current, patch)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=2) + "\n")
        tmp.replace(config_path)
        note = f" — {args['reason']}" if args.get("reason") else ""
        return _ok(f"config updated{note}")

    return [read_config, write_config]


__all__ = ["build"]
