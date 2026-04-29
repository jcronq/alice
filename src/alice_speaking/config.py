"""Loads Alice's runtime configuration.

Two sources, by design:
- ``alice.env``: secrets + environment-level wiring (signal account, API endpoint,
  OAuth token, paths). Already exists; shared with the legacy bash bridge.
- ``alice.config.json`` (in alice-mind): behavioral knobs Alice can self-tune.
  Optional in phase 1 — defaults kick in when absent.

A third source is loaded by the daemon directly (not the Config object):
``principals.yaml`` (in alice-mind/config/) — the address book / ACL.
See :mod:`alice_speaking.principals`.
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
# CLI transport socket. Lives in the container's local filesystem (not on
# a bind-mounted volume) — bind mounts from macOS / Rancher Desktop are
# served via virtiofs/9p, which doesn't support AF_UNIX socket files
# (bind() returns EPERM). The socket is ephemeral anyway: the daemon
# unlinks any stale path and rebinds on every restart. Override with
# ALICE_CLI_SOCKET in alice.env or the environment.
DEFAULT_CLI_SOCKET = pathlib.Path("/tmp/alice.sock")

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
    # How many recent turns from speaking-turns.jsonl to inject as the Layer 2
    # bootstrap preamble when Layer 1 (session_id resume) fails or is missing.
    # See design-unified-context-compaction.md.
    "context_bootstrap_turns": 20,
    # When ResultMessage.usage.input_tokens exceeds this value, flag the
    # session for compaction. On the next event, run a summary turn, roll the
    # session, and inject the summary + tail(5) turns as preamble. 150K ~= 75%
    # of a 200K window — leaves runway for the compaction turn itself.
    "context_compaction_threshold": 150_000,
}


@dataclass
class Config:
    # From alice.env
    signal_api: str
    # Empty string when Signal is disabled (no SIGNAL_ACCOUNT in alice.env).
    # The daemon skips SignalClient/SignalTransport construction in that case
    # and the CLI / Discord transports run on their own.
    signal_account: str
    # Empty string when no token is in alice.env or the env. The Claude
    # Code SDK falls back to ~/.claude/.credentials.json (the entrypoint
    # symlinks it from the host), so this isn't strictly required.
    oauth_token: str
    work_dir: pathlib.Path

    # Paths (derived, overridable)
    mind_dir: pathlib.Path
    state_dir: pathlib.Path
    signal_log_path: pathlib.Path
    offset_path: pathlib.Path
    seen_path: pathlib.Path
    turn_log_path: pathlib.Path
    event_log_path: pathlib.Path

    # Address book / ACL — path to principals.yaml plus the parsed
    # ALLOWED_SENDERS env var, kept around as the synth-fallback input
    # when the YAML doesn't exist yet. Once a deploy authors
    # principals.yaml, ``allowed_senders_fallback`` becomes irrelevant.
    principals_path: pathlib.Path = field(
        default_factory=lambda: DEFAULT_MIND_DIR / "config" / "principals.yaml"
    )
    allowed_senders_fallback: dict[str, str] = field(default_factory=dict)

    # CLI transport
    cli_enabled: bool = True
    cli_socket_path: pathlib.Path = field(default_factory=lambda: DEFAULT_CLI_SOCKET)

    # Discord transport — optional. When ``discord_bot_token`` is empty
    # the daemon skips construction; existing deploys without Discord
    # configured keep working.
    discord_bot_token: str = ""

    # API-key auth mode. When ``anthropic_base_url`` or ``anthropic_api_key``
    # is set, alice_core.auth picks "api" mode and routes the CLI through
    # this endpoint instead of the default Claude subscription flow. Used
    # for LiteLLM proxies (or direct Anthropic API).
    anthropic_base_url: str = ""
    anthropic_api_key: str = ""
    anthropic_auth_token: str = ""

    # A2A transport — optional. When ``a2a_enabled`` is False the daemon
    # skips construction. A2A lets external (Google A2A protocol)
    # agents submit tasks to Alice over HTTP/JSON-RPC; the worker
    # exposes a port that compose maps to the host.
    a2a_enabled: bool = False
    a2a_port: int = 7878
    a2a_host: str = "0.0.0.0"
    # Single shared principal for all A2A traffic in v1. Operator points
    # an upstream proxy (oauth2-proxy / Caddy / etc.) at the worker port
    # and fronts it with whatever auth their org uses; per-caller
    # principal lookup is a follow-up.
    a2a_principal: str = "a2a"
    # URL advertised on the agent card at /.well-known/agent-card.json.
    # Empty defaults to ``http://<a2a_host>:<a2a_port>/`` — fine for
    # local dev. Set to the public URL when fronted by a reverse proxy.
    a2a_external_url: str = ""

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


def _parse_allowed_senders(raw: str) -> dict[str, str]:
    """Parse the legacy ``ALLOWED_SENDERS`` env var into a ``{number: name}``
    mapping. Used as the synth-fallback input for the address book when
    ``principals.yaml`` is absent.

    Format: ``"+15555550100:Owner,+15555550101:Friend"``.
    """
    senders: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        number, _, name = pair.partition(":")
        number = number.strip()
        name = name.strip()
        if number and name:
            senders[number] = name
    return senders


def load() -> Config:
    env_path = pathlib.Path(os.environ.get("ALICE_CONFIG", DEFAULT_ALICE_ENV))
    env = _load_env_file(env_path)

    # alice.env holds secrets + long-lived identity; compose injects
    # container-level wiring (SIGNAL_API, STATE_DIR, SIGNAL_LOG_FILE) as
    # environment vars. Prefer the env file for secrets, env vars for wiring.
    def from_any(key: str, default: str | None = None) -> str | None:
        return env.get(key) or os.environ.get(key) or default

    signal_api = from_any("SIGNAL_API", "http://127.0.0.1:8080") or ""
    # Signal is opt-in. When SIGNAL_ACCOUNT is unset, the daemon skips the
    # transport entirely; CLI + Discord still work standalone.
    signal_account = from_any("SIGNAL_ACCOUNT", "") or ""
    # Claude OAuth token: prefer alice.env, fall back to the symlinked
    # ~/.claude/.credentials.json that the entrypoint maintains. Empty
    # here means "let the SDK find it on disk."
    oauth_token = from_any("CLAUDE_CODE_OAUTH_TOKEN", "") or ""
    # API-key mode (LiteLLM or direct Anthropic API). All three optional;
    # presence of base_url or api_key flips alice_core.auth into "api" mode.
    anthropic_base_url = from_any("ANTHROPIC_BASE_URL", "") or ""
    anthropic_api_key = from_any("ANTHROPIC_API_KEY", "") or ""
    anthropic_auth_token = from_any("ANTHROPIC_AUTH_TOKEN", "") or ""
    allowed = _parse_allowed_senders(from_any("ALLOWED_SENDERS", "") or "")
    work_dir = pathlib.Path(from_any("WORK_DIR", str(DEFAULT_MIND_DIR)) or str(DEFAULT_MIND_DIR))

    mind_dir = pathlib.Path(from_any("ALICE_MIND_DIR", str(work_dir)) or str(work_dir))
    state_dir = pathlib.Path(from_any("STATE_DIR", str(DEFAULT_STATE_DIR)) or str(DEFAULT_STATE_DIR))
    signal_log = pathlib.Path(
        from_any("SIGNAL_LOG_FILE")
        or str(state_dir.parent / "daemon" / "signal-daemon.log")
    )

    speaking = dict(SPEAKING_DEFAULTS)
    config_json = mind_dir / "config" / "alice.config.json"
    if config_json.is_file():
        try:
            parsed = json.loads(config_json.read_text())
            speaking.update(parsed.get("speaking") or {})
        except json.JSONDecodeError as exc:
            raise ValueError(f"{config_json} is not valid JSON: {exc}") from exc

    cli_enabled_raw = (from_any("ALICE_CLI_ENABLED", "1") or "1").strip().lower()
    cli_enabled = cli_enabled_raw not in {"0", "false", "no", "off", ""}
    cli_socket_path = pathlib.Path(
        from_any("ALICE_CLI_SOCKET", str(DEFAULT_CLI_SOCKET))
        or str(DEFAULT_CLI_SOCKET)
    )

    principals_path = pathlib.Path(
        from_any("ALICE_PRINCIPALS_FILE", str(mind_dir / "config" / "principals.yaml"))
        or str(mind_dir / "config" / "principals.yaml")
    )

    discord_bot_token = (from_any("DISCORD_BOT_TOKEN", "") or "").strip()

    a2a_enabled_raw = (from_any("ALICE_A2A_ENABLED", "0") or "0").strip().lower()
    a2a_enabled = a2a_enabled_raw in {"1", "true", "yes", "on"}
    try:
        a2a_port = int(from_any("ALICE_A2A_PORT", "7878") or "7878")
    except ValueError:
        a2a_port = 7878
    a2a_host = (from_any("ALICE_A2A_HOST", "0.0.0.0") or "0.0.0.0").strip()
    a2a_principal = (from_any("ALICE_A2A_PRINCIPAL", "a2a") or "a2a").strip()
    a2a_external_url = (from_any("ALICE_A2A_EXTERNAL_URL", "") or "").strip()

    return Config(
        signal_api=signal_api,
        signal_account=signal_account,
        oauth_token=oauth_token,
        anthropic_base_url=anthropic_base_url,
        anthropic_api_key=anthropic_api_key,
        anthropic_auth_token=anthropic_auth_token,
        work_dir=work_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=signal_log,
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen-timestamps",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        event_log_path=pathlib.Path(
            from_any("SPEAKING_EVENT_LOG") or str(state_dir / "speaking.log")
        ),
        principals_path=principals_path,
        allowed_senders_fallback=allowed,
        speaking=speaking,
        cli_enabled=cli_enabled,
        cli_socket_path=cli_socket_path,
        discord_bot_token=discord_bot_token,
        a2a_enabled=a2a_enabled,
        a2a_port=a2a_port,
        a2a_host=a2a_host,
        a2a_principal=a2a_principal,
        a2a_external_url=a2a_external_url,
    )
