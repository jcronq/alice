"""Auth-env loading — unified across hemispheres.

Two auth modes are supported:

- ``subscription`` — long-lived OAuth token from Anthropic's web flow,
  stored in ``alice.env`` as ``CLAUDE_CODE_OAUTH_TOKEN``. The Claude
  Code CLI hits api.anthropic.com using subscription billing.
- ``api`` — direct API key, optionally routed through a LiteLLM (or
  any Anthropic-compatible) proxy. Set in ``alice.env`` as
  ``ANTHROPIC_BASE_URL`` (proxy endpoint; omit for direct Anthropic
  API), ``ANTHROPIC_API_KEY``, and optionally ``ANTHROPIC_AUTH_TOKEN``
  (bearer-token style proxy auth).

The mode is picked implicitly: presence of ``ANTHROPIC_BASE_URL`` or
``ANTHROPIC_API_KEY`` selects ``api``; otherwise ``subscription``. When
``api`` is active we explicitly clear ``CLAUDE_CODE_OAUTH_TOKEN`` from
the subprocess env — the CLI gets confused if both are set.

The Agent SDK reads these vars from the subprocess environment, so
:func:`ensure_auth_env` mutates ``os.environ`` once at process startup
and the SDK call inherits the result.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Literal


DEFAULT_ALICE_ENV = pathlib.Path.home() / ".config" / "alice" / "alice.env"

AuthMode = Literal["subscription", "api", "none"]

_API_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@dataclass(frozen=True)
class AuthEnv:
    """Auth settings resolved from env + alice.env."""

    mode: AuthMode
    oauth_token: str = ""
    api_key: str = ""
    auth_token: str = ""
    base_url: str = ""


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


def find_auth_env(env_file: pathlib.Path | None = None) -> AuthEnv:
    """Resolve auth settings from process env and alice.env (in that order).

    Returns an :class:`AuthEnv` with ``mode`` set based on which vars are
    populated. ``mode == "none"`` means nothing was found — the caller
    decides whether that's fatal (the SDK can still fall back to a host
    ``~/.claude/.credentials.json`` symlinked into the container).
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

    if base_url or api_key or auth_token:
        mode: AuthMode = "api"
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
    )


def ensure_auth_env(env_file: pathlib.Path | None = None) -> AuthEnv:
    """Resolve auth settings and write the right vars into ``os.environ``.

    The Claude Code CLI subprocess inherits the parent environment, so we
    set what we want it to see and clear what we don't. In ``subscription``
    mode this means ``CLAUDE_CODE_OAUTH_TOKEN`` set, ``ANTHROPIC_*`` clear.
    In ``api`` mode it's the inverse.
    """
    auth = find_auth_env(env_file)

    if auth.mode == "api":
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
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
