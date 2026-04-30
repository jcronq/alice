"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.domain.turn_log` (Plan 02).
"""

from .domain.turn_log import *  # noqa: F401,F403
from .domain.turn_log import (  # noqa: F401
    Turn,
    TurnLog,
    new_turn,
    render_for_prompt,
)
