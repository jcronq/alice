"""Loads Alice's runtime configuration.

Two sources, by design:
- ``alice.env``: secrets + environment-level wiring (signal account, API endpoint,
  OAuth token, paths). Already exists; shared with the legacy bash bridge.
- ``alice.config.json`` (in alice-mind): behavioral knobs Alice can self-tune.
  Optional in phase 1 — defaults kick in when absent.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any


DEFAULT_ALICE_ENV = pathlib.Path.home() / ".config" / "alice" / "alice.env"
DEFAULT_MIND_DIR = pathlib.Path.home() / "alice-mind"
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")

# Fallback speaking-hemisphere config, applied when alice.config.json is absent
# or omits fields. Matches the defaults in HEMISPHERES.md.
SPEAKING_DEFAULTS: dict[str, Any] = {
    "model": "claude-opus-4-7",
    "always_thinking": True,
    "working_context_token_budget": 2000,
    "rate_limit_policy": {
        "retry": True,
        "notify_user_after_seconds": 30,
    },
    "proactive_messages_allowed": True,
    "quiet_hours": {
        "start": "22:00",
        "end": "07:00",
        "timezone": "America/New_York",
    },
}


@dataclass
class AllowedSender:
    number: str
    name: str


@dataclass
class Config:
    # From alice.env
    signal_api: str
    signal_account: str
    oauth_token: str
    allowed_senders: dict[str, AllowedSender]
    work_dir: pathlib.Path

    # Paths (derived, overridable)
    mind_dir: pathlib.Path
    state_dir: pathlib.Path
    signal_log_path: pathlib.Path
    offset_path: pathlib.Path
    seen_path: pathlib.Path
    turn_log_path: pathlib.Path

    # Behavior (from alice.config.json, falls back to SPEAKING_DEFAULTS)
    speaking: dict[str, Any] = field(default_factory=lambda: dict(SPEAKING_DEFAULTS))


def _load_env_file(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"alice.env not found at {path}")
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _parse_allowed_senders(raw: str) -> dict[str, AllowedSender]:
    # Format: "+15555550100:Owner,+15555550101:Friend"
    senders: dict[str, AllowedSender] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        number, _, name = pair.partition(":")
        number = number.strip()
        name = name.strip()
        if number and name:
            senders[number] = AllowedSender(number=number, name=name)
    return senders


def load() -> Config:
    env_path = pathlib.Path(os.environ.get("ALICE_CONFIG", DEFAULT_ALICE_ENV))
    env = _load_env_file(env_path)

    def required(key: str) -> str:
        value = env.get(key) or os.environ.get(key)
        if not value:
            raise KeyError(f"{key} missing from {env_path}")
        return value

    signal_api = env.get("SIGNAL_API", "http://127.0.0.1:8080")
    signal_account = required("SIGNAL_ACCOUNT")
    oauth_token = required("CLAUDE_CODE_OAUTH_TOKEN")
    allowed = _parse_allowed_senders(required("ALLOWED_SENDERS"))
    work_dir = pathlib.Path(env.get("WORK_DIR") or DEFAULT_MIND_DIR)

    mind_dir = pathlib.Path(env.get("ALICE_MIND_DIR") or work_dir)
    state_dir = pathlib.Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR)
    signal_log = pathlib.Path(
        env.get("SIGNAL_LOG_FILE") or state_dir.parent / "daemon" / "signal-daemon.log"
    )

    speaking = dict(SPEAKING_DEFAULTS)
    config_json = mind_dir / "config" / "alice.config.json"
    if config_json.is_file():
        try:
            parsed = json.loads(config_json.read_text())
            speaking.update(parsed.get("speaking") or {})
        except json.JSONDecodeError as exc:
            raise ValueError(f"{config_json} is not valid JSON: {exc}") from exc

    return Config(
        signal_api=signal_api,
        signal_account=signal_account,
        oauth_token=oauth_token,
        allowed_senders=allowed,
        work_dir=work_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=signal_log,
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen-timestamps",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        speaking=speaking,
    )
