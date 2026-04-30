"""Deprecated. Re-export shim — the real module is
:mod:`alice_speaking.infra.signal_rpc` (Plan 02).

The class formerly named ``SignalClient`` is now ``SignalRPC`` —
the alias below preserves the old class name for callers that
haven't migrated yet.
"""

from .infra.signal_rpc import *  # noqa: F401,F403
from .infra.signal_rpc import (  # noqa: F401
    Attachment,
    SignalEnvelope,
    SignalRPC,
    _parse_attachments,  # noqa: F401  underscore-prefixed; explicit
    _parse_envelope,     # noqa: F401  so the shim forwards the
                         #              test-only helpers too.
)

# Back-compat alias: the symbol is also exposed under its old name
# so callers that imported ``SignalClient`` keep working.
SignalClient = SignalRPC
