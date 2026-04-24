"""Backwards-compat shim — re-exports from :mod:`alice_core.session`.

The canonical location moved to alice_core in step 5 of the kernel
refactor. Tests and daemon.py import from this shim so nothing has to
change on either side during the migration.
"""

from __future__ import annotations

from alice_core.session import (
    PersistedSession,
    clear,
    read,
    sdk_session_exists,
    sdk_session_jsonl_path,
    write,
)


__all__ = [
    "PersistedSession",
    "write",
    "read",
    "clear",
    "sdk_session_jsonl_path",
    "sdk_session_exists",
]
