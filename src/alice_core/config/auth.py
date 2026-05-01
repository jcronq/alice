"""Auth-env loading — unified across hemispheres.

Three auth modes are supported (Plan 06 added the third):

- ``subscription`` — long-lived OAuth token from Anthropic's web flow,
  stored in ``alice.env`` as ``CLAUDE_CODE_OAUTH_TOKEN``. The Claude
  Code CLI hits api.anthropic.com using subscription billing.
- ``api`` — direct API key, optionally routed through a LiteLLM (or
  any Anthropic-compatible) proxy. Set in ``alice.env`` as
  ``ANTHROPIC_BASE_URL`` (proxy endpoint; omit for direct Anthropic
  API), ``ANTHROPIC_API_KEY``, and optionally ``ANTHROPIC_AUTH_TOKEN``
  (bearer-token style proxy auth).
- ``bedrock`` — AWS Bedrock via ``CLAUDE_CODE_USE_BEDROCK=1``. AWS
  credentials flow through the standard boto3 credential chain
  (env vars, ``~/.aws/credentials``, EC2 instance profile).
  ``auth.py`` only manages the SDK-facing flag + region/profile
  hints; it doesn't touch ``AWS_ACCESS_KEY_ID`` etc.

Without a ``mode_hint`` argument the mode is picked implicitly:
presence of ``ANTHROPIC_BASE_URL`` or ``ANTHROPIC_API_KEY`` selects
``api``; ``CLAUDE_CODE_OAUTH_TOKEN`` selects ``subscription``;
otherwise ``none``. With a ``mode_hint`` the caller chooses
explicitly — Plan 06 wires this from ``mind/config/model.yml`` so a
hemisphere can declare ``backend: bedrock`` without setting any env
vars first.

The Agent SDK reads these vars from the subprocess environment, so
:func:`ensure_auth_env` mutates ``os.environ`` once at process startup
and the SDK call inherits the result.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Literal, Optional


DEFAULT_ALICE_ENV = pathlib.Path.home() / ".config" / "alice" / "alice.env"

AuthMode = Literal["subscription", "api", "bedrock", "none", "pi"]

_API_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_BEDROCK_VARS = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_PROFILE")


@dataclass(frozen=True)
class AuthEnv:
    """Auth settings resolved from env + alice.env."""

    mode: AuthMode
    oauth_token: str = ""
    api_key: str = ""
    auth_token: str = ""
    base_url: str = ""
    aws_region: str = ""
    aws_profile: str = ""


def _resolve_env_path(env_file: pathlib.Path | None) -> pathlib.Path | None:
    """Pick which env file to read, preferring explicit > ALICE_CONFIG > default."""
    if env_file is not None and env_file.is_file():
        return env_file
    candidate = os.environ.get("ALICE_CONFIG")
    if candidate:
        path = pathlib.Path(candidate)
        if path.is_file():
            return path
    if DEFAULT_ALICE_ENV.is_file():
        return DEFAULT_ALICE_ENV
    return None


def _load_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def find_auth_env(
    env_file: pathlib.Path | None = None,
    *,
    mode_hint: Optional[AuthMode] = None,
    aws_region: str = "",
    aws_profile: str = "",
) -> AuthEnv:
    """Resolve auth settings from process env and alice.env (in that order).

    Returns an :class:`AuthEnv` with ``mode`` set based on which vars are
    populated. ``mode == "none"`` means nothing was found — the caller
    decides whether that's fatal (the SDK can still fall back to a host
    ``~/.claude/.credentials.json`` symlinked into the container).

    ``mode_hint`` lets a caller pin the mode explicitly (Plan 06's
    ``model.yml``: hemispheres declare ``backend: bedrock`` and pass
    that down). When ``mode_hint`` is ``None`` the implicit-from-env
    logic above runs unchanged so minds without ``model.yml`` keep
    working. ``aws_region`` / ``aws_profile`` only matter for
    ``mode_hint == "bedrock"``.
    """
    file_env: dict[str, str] = {}
    path = _resolve_env_path(env_file)
    if path is not None:
        file_env = _load_env_file(path)

    def pick(key: str) -> str:
        return os.environ.get(key) or file_env.get(key, "") or ""

    base_url = pick("ANTHROPIC_BASE_URL")
    api_key = pick("ANTHROPIC_API_KEY")
    auth_token = pick("ANTHROPIC_AUTH_TOKEN")
    oauth_token = pick("CLAUDE_CODE_OAUTH_TOKEN")
    region = aws_region or pick("AWS_REGION")
    profile = aws_profile or pick("AWS_PROFILE")

    mode: AuthMode
    if mode_hint is not None:
        mode = mode_hint
    elif base_url or api_key or auth_token:
        mode = "api"
    elif oauth_token:
        mode = "subscription"
    else:
        mode = "none"

    return AuthEnv(
        mode=mode,
        oauth_token=oauth_token,
        api_key=api_key,
        auth_token=auth_token,
        base_url=base_url,
        aws_region=region,
        aws_profile=profile,
    )


def ensure_auth_env(
    env_file: pathlib.Path | None = None,
    *,
    mode_hint: Optional[AuthMode] = None,
    aws_region: str = "",
    aws_profile: str = "",
) -> AuthEnv:
    """Resolve auth settings and write the right vars into ``os.environ``.

    The Claude Code CLI subprocess inherits the parent environment, so we
    set what we want it to see and clear what we don't. ``subscription``
    mode sets ``CLAUDE_CODE_OAUTH_TOKEN`` and clears the rest;
    ``api`` mode sets ``ANTHROPIC_*`` and clears the rest;
    ``bedrock`` mode sets ``CLAUDE_CODE_USE_BEDROCK=1`` (+ optional
    ``AWS_REGION`` / ``AWS_PROFILE``) and clears the rest. Note that
    bedrock mode doesn't touch ``AWS_ACCESS_KEY_ID`` etc. — boto3's
    credential chain handles those.

    ``mode_hint``, ``aws_region``, ``aws_profile`` are forwarded to
    :func:`find_auth_env` to support Plan 06's per-hemisphere
    backend selection.
    """
    auth = find_auth_env(
        env_file,
        mode_hint=mode_hint,
        aws_region=aws_region,
        aws_profile=aws_profile,
    )

    if auth.mode == "api":
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        for key in _BEDROCK_VARS:
            os.environ.pop(key, None)
        for key, value in (
            ("ANTHROPIC_BASE_URL", auth.base_url),
            ("ANTHROPIC_API_KEY", auth.api_key),
            ("ANTHROPIC_AUTH_TOKEN", auth.auth_token),
        ):
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
    elif auth.mode == "subscription":
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = auth.oauth_token
        for key in _API_VARS:
            os.environ.pop(key, None)
        for key in _BEDROCK_VARS:
            os.environ.pop(key, None)
    elif auth.mode == "bedrock":
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if auth.aws_region:
            os.environ["AWS_REGION"] = auth.aws_region
        if auth.aws_profile:
            os.environ["AWS_PROFILE"] = auth.aws_profile
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        for key in _API_VARS:
            os.environ.pop(key, None)
    elif auth.mode == "pi":
        # Pi-coding-agent reads its own auth file (~/.pi/agent/auth.json)
        # populated by the codex→pi bridge in the container entrypoint.
        # Clear the Anthropic-specific env vars so a stale subscription
        # token / API key doesn't accidentally re-enter when Anthropic
        # backends run later in the same process.
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        for key in _API_VARS:
            os.environ.pop(key, None)
        for key in _BEDROCK_VARS:
            os.environ.pop(key, None)
    # mode == "none": leave environment untouched; SDK falls back to
    # ~/.claude/.credentials.json (entrypoint symlinks it into the container).

    return auth


def find_token(env_file: pathlib.Path | None = None) -> str | None:
    """Back-compat shim: return the OAuth token if available, else ``None``.

    New code should use :func:`find_auth_env` to also see the API-mode
    vars. Kept so older imports keep working.
    """
    auth = find_auth_env(env_file)
    return auth.oauth_token or None


def ensure_token(env_file: pathlib.Path | None = None) -> str | None:
    """Back-compat shim: alias for :func:`ensure_auth_env` that returns the
    OAuth token (or ``None``).

    Existing callsites use this to set ``CLAUDE_CODE_OAUTH_TOKEN`` on the
    process env. The new function does that *and* the API-mode vars, so
    leaving callsites on the shim is safe — they just gain API support
    transparently.
    """
    auth = ensure_auth_env(env_file)
    return auth.oauth_token or None


__all__ = [
    "AuthEnv",
    "AuthMode",
    "DEFAULT_ALICE_ENV",
    "ensure_auth_env",
    "ensure_token",
    "find_auth_env",
    "find_token",
]
