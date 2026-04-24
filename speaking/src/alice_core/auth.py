"""OAuth token loading — unified across hemispheres.

Claude Code reads the OAuth token from the ``CLAUDE_CODE_OAUTH_TOKEN``
environment variable. Historically, two code paths independently
re-implemented the "env first, then ``alice.env`` fallback" logic:

- ``alice_speaking.think._load_token`` — reads env, then scans the env
  file line by line for the ``CLAUDE_CODE_OAUTH_TOKEN=`` prefix.
- ``alice_speaking.config._load_env_file`` + ``load()`` — reads the
  whole env file into a dict, picks up ``CLAUDE_CODE_OAUTH_TOKEN`` (and
  everything else) from there.

This module consolidates both into one lookup function + a helper that
also mutates ``os.environ`` when the SDK expects the variable to be
present on the subprocess environment.
"""

from __future__ import annotations

import os
import pathlib


DEFAULT_ALICE_ENV = pathlib.Path.home() / ".config" / "alice" / "alice.env"


def find_token(env_file: pathlib.Path | None = None) -> str | None:
    """Return the OAuth token from the environment or the ``alice.env`` file.

    Resolution order:

    1. ``CLAUDE_CODE_OAUTH_TOKEN`` env var (non-empty).
    2. ``ALICE_CONFIG`` env var pointing at an env file (if set).
    3. ``env_file`` argument (if given).
    4. ``~/.config/alice/alice.env``.

    Returns ``None`` if no source yields a value.
    """
    value = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if value:
        return value

    candidate = (
        env_file
        or pathlib.Path(os.environ.get("ALICE_CONFIG", ""))
        or DEFAULT_ALICE_ENV
    )
    if not candidate or not candidate.is_file():
        candidate = DEFAULT_ALICE_ENV
    if not candidate.is_file():
        return None

    try:
        for raw in candidate.read_text().splitlines():
            line = raw.strip()
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def ensure_token(env_file: pathlib.Path | None = None) -> str | None:
    """Look up the token and write it back into ``os.environ`` if found.

    The Agent SDK subprocesses ``claude``; inheriting this env var is how
    authentication flows through. Callers should invoke this once at
    startup so child processes see the right value.

    Returns the token (or ``None`` if unresolved). The caller decides
    whether a missing token is fatal — some entry points (e.g. tests)
    may not need one.
    """
    token = find_token(env_file)
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return token


__all__ = ["find_token", "ensure_token", "DEFAULT_ALICE_ENV"]
