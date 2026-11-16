"""Presence check for the status-site browser tab.

Slice: presence-aware Signal notifications. ``check_focused`` hits
``GET /api/presence/status`` on the local status-site container and
reports whether Jason's status-site tab heartbeated within its
window. Used by :mod:`alice_speaking.daemon` to suppress the
Signal ping on a status-chat reply when the tab is actively
focused.

Fail-open by design: any error, timeout, bad JSON or non-200 means
"not focused" so the Signal ping fires normally. Presence
suppression is a nice-to-have, not a guarantee — never drop a ping
just because the status-site had a hiccup.

Stdlib only — this module is imported by the speaking daemon (and
its test suite); adding ``httpx`` or similar just for a 1 s GET
would be gratuitous.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


#: Hard cap on the presence GET. The status-site handler is a
#: dict snapshot under a short lock — a 1 s budget is many orders
#: of magnitude more than the happy-path and still well under any
#: user-visible delay on the Signal path.
DEFAULT_TIMEOUT_S: float = 1.0

#: Default to the status-site docker DNS name on ``sandbox_default``
#: — ``alice`` and ``alice-status`` share that network, so the
#: lookup stays inside the compose bridge (no trip through the LAN
#: IP). Override with ``ALICE_PRESENCE_URL`` for anything else.
_FALLBACK_URL: str = "http://status-site:8095/api/presence/status"


def _default_url() -> str:
    # Read at call time so test monkeypatches of ``os.environ`` land
    # without having to re-import the module.
    return os.environ.get("ALICE_PRESENCE_URL", _FALLBACK_URL)


#: Back-compat alias — older import sites that read ``DEFAULT_URL``
#: as a constant get the current value at import time.
DEFAULT_URL: str = _default_url()


@dataclass(frozen=True)
class PresenceReading:
    """Normalised view of ``GET /api/presence/status``.

    ``focused`` is the only thing callers actually need; the other
    fields are plumbed through so a log line can say *why* a
    suppression happened without a second round-trip.
    """

    focused: bool
    focused_within_s: float | None
    error: str | None = None

    @classmethod
    def not_focused(cls, *, error: str | None = None) -> "PresenceReading":
        return cls(focused=False, focused_within_s=None, error=error)


def check_focused(
    url: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: urllib.request.OpenerDirector | None = None,
) -> PresenceReading:
    """Return the current presence reading. Never raises.

    ``url`` defaults to ``$ALICE_PRESENCE_URL`` (resolved at call
    time) when not supplied. The ``opener`` hook is only for tests
    — real callers pass neither and we use
    ``urllib.request.urlopen`` directly.
    """
    if url is None:
        url = _default_url()
    try:
        req = urllib.request.Request(url, method="GET")
        if opener is None:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = opener.open(req, timeout=timeout)
        with resp:
            if getattr(resp, "status", 200) != 200:
                return PresenceReading.not_focused(
                    error=f"http {resp.status}"
                )
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return PresenceReading.not_focused(error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — fail-open invariant
        log.debug("presence check: unexpected %s: %s", type(exc).__name__, exc)
        return PresenceReading.not_focused(error=f"{type(exc).__name__}: {exc}")

    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        return PresenceReading.not_focused(error=f"bad json: {exc}")

    if not isinstance(data, dict):
        return PresenceReading.not_focused(error="bad shape")
    # Strict ``is True`` — reject the fail-closed corner where a
    # malformed server returns ``"focused": "false"`` (truthy string)
    # or any other non-bool truthy value (review finding #6). Also
    # excludes ``bool`` masquerading as ``int`` below because
    # ``isinstance(True, int)`` would otherwise slip through.
    focused = data.get("focused") is True
    within_raw = data.get("focused_within_s")
    if isinstance(within_raw, bool) or not isinstance(within_raw, (int, float)):
        within = None
    else:
        within = float(within_raw)
    return PresenceReading(focused=focused, focused_within_s=within)
