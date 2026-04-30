"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.domain.principals` (Plan 02).
"""

from .domain.principals import *  # noqa: F401,F403
from .domain.principals import (  # noqa: F401
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
    load,
)
