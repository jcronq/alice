"""Deprecated. Re-export shim — the real module is
:mod:`alice_core.config.auth` (Plan 08 Phase 2 of the refactor).
Phase 3 retires this shim.
"""

from .config.auth import *  # noqa: F401,F403
from .config.auth import (  # noqa: F401
    AuthEnv,
    AuthMode,
    DEFAULT_ALICE_ENV,
    ensure_auth_env,
    ensure_token,
    find_auth_env,
    find_token,
)
