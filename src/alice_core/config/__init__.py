"""Configuration loaders — auth today, personae + model arrive next.

Plan 08 Phase 2 of the refactor lifts the env-var auth resolver out
of ``alice_core/auth.py`` into this subpackage. Plans 05 and 06
land here next: ``personae.py`` (agent + user identity) and
``model.py`` (backend selection — subscription / api / Bedrock /
Vertex). Same shape across all three: read source → typed frozen
dataclass → loader function. Same parent so the directory listing
communicates intent.

The runtime imports configuration loaders via this package. The
shim at ``alice_core/auth.py`` is a temporary back-compat hook;
plan 08 phase 3 retires it once callers migrate to the new path.
"""

from .auth import (
    AuthEnv,
    AuthMode,
    DEFAULT_ALICE_ENV,
    ensure_auth_env,
    ensure_token,
    find_auth_env,
    find_token,
)


__all__ = [
    "AuthEnv",
    "AuthMode",
    "DEFAULT_ALICE_ENV",
    "ensure_auth_env",
    "ensure_token",
    "find_auth_env",
    "find_token",
]
