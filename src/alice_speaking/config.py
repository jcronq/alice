"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.infra.config` (Plan 02).
"""

from .infra.config import *  # noqa: F401,F403
from .infra.config import (  # noqa: F401
    Config,
    DEFAULT_ALICE_ENV,
    DEFAULT_CLI_SOCKET,
    DEFAULT_MIND_DIR,
    DEFAULT_STATE_DIR,
    SPEAKING_DEFAULTS,
    load,
)
