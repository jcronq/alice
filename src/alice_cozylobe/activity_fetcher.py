"""Activity fetcher — periodic "what's been happening" snapshot from
cozyhem-engine.

Companion to :mod:`alice_cozylobe.sse_consumer`. The SSE consumer is
push-driven and silent between events; the fetcher is pull-driven and
gives the periodic wake mode a substrate to reason about even when the
home is quiet (no doorbell, no motion, no entity changes).

The fetcher hits three endpoints already exposed by cozyhem-engine
(see ``cozyhem/api/routes/``):

* ``GET /api/v1/entities/states`` — all entities with their current
  states. Rich; the snapshot trims this to a compact dict so the
  reasoning prompt stays under budget.
* ``GET /api/v1/anthem/status`` — Anthem receiver power / volume /
  connected.
* ``GET /api/v1/lights/`` — managed-light states (on/off, brightness,
  ct, reachable).

We deliberately do NOT introduce a new ``/api/v1/logs`` route on
cozyhem-engine in this PR — that's a follow-up in cozyhem-engine if
the periodic prompt turns out to need a real activity-log feed.

**Graceful degrade** (design's ``lobe-quiet-on-link-loss`` rule):
on any HTTP / network failure :meth:`ActivityFetcher.fetch` returns
``None`` and logs ONCE per outage. The wake loop's periodic tick
treats ``None`` as "skip this tick" — it does NOT dispatch run_agent
with a null snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from .sse_consumer import DEFAULT_EVENTS_URL


__all__ = [
    "ActivitySnapshot",
    "ActivityFetcher",
    "DEFAULT_COZYHEM_BASE_URL",
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "base_url_from_events_url",
]


log = logging.getLogger(__name__)


DEFAULT_FETCH_TIMEOUT_SECONDS = 10.0


def base_url_from_events_url(events_url: str) -> str:
    """Strip the ``/api/v1/events`` tail off the SSE URL to recover the
    cozyhem-engine origin.

    Mirrors the convention used in
    :class:`alice_speaking.internal.cozyhem.CozyHemEventSubscriber`:
    the SSE URL is the canonical anchor and the REST endpoints share
    its host + port. Returns the origin (scheme + netloc) so callers
    can append ``/api/v1/<route>`` themselves.
    """
    parsed = urlparse(events_url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


# Derived once at import so the daemon's default constructor doesn't
# re-parse on every wake.
DEFAULT_COZYHEM_BASE_URL = base_url_from_events_url(DEFAULT_EVENTS_URL)


@dataclass(frozen=True)
class ActivitySnapshot:
    """Compact view of the home's current state.

    Each field is the response body from the corresponding cozyhem
    endpoint — kept loosely typed (``dict`` / ``list``) on purpose so
    the periodic prompt stays robust to additive schema changes
    upstream. A missing-field entry stays ``None`` rather than empty
    so the prompt builder can distinguish "endpoint failed" from
    "endpoint returned nothing meaningful".

    Attributes:
        entity_states: ``GET /api/v1/entities/states`` body
            (typically a dict of ``entity_id -> state``). ``None`` if
            that single endpoint failed but the snapshot as a whole is
            still useful.
        anthem_status: ``GET /api/v1/anthem/status`` body.
        lights: ``GET /api/v1/lights/`` body.
        fetched_at: ``time.time()`` at the moment all sub-fetches
            completed (or were attempted). Useful for staleness
            checks in the periodic prompt.
    """

    entity_states: Optional[Any] = None
    anthem_status: Optional[Any] = None
    lights: Optional[Any] = None
    fetched_at: float = 0.0
    # Per-endpoint error markers. Empty list = clean snapshot. Non-
    # empty = partial degrade; the snapshot is still returned so the
    # periodic prompt can reason about whichever fields landed.
    partial_errors: list[str] = field(default_factory=list)


class ActivityFetcher:
    """HTTP client that pulls a snapshot of recent CozyHem activity.

    Inject ``http_client_factory`` in tests so we don't open real
    sockets. The factory returns an :class:`httpx.AsyncClient`-shaped
    async context manager.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_COZYHEM_BASE_URL,
        *,
        timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
        http_client_factory: Optional[
            Callable[[], Any]
        ] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )
        if clock is None:
            import time as _time

            clock = _time.time
        self._clock = clock
        # Track whether we've already logged an unreachable warning in
        # the current outage. Reset on first successful fetch so a
        # flapping endpoint surfaces every time it recovers.
        self._unreachable_warned = False

    async def fetch(self) -> Optional[ActivitySnapshot]:
        """Pull all snapshot endpoints concurrently and assemble the
        result. Returns ``None`` if every endpoint fails (cozyhem
        unreachable). Returns a partial snapshot if some endpoints
        succeed and others fail — partial is still useful for
        reasoning, and the failures are recorded in
        :attr:`ActivitySnapshot.partial_errors`.

        Logs ONCE per outage when the entire snapshot is unreachable;
        the wake loop's periodic tick skips on ``None``.
        """
        # TODO(cozyhem-engine#???): if the periodic prompt ends up
        # needing a real activity-log feed (entity-change history,
        # automation firings), file an issue on cozyhem-engine to add
        # GET /api/v1/logs. For the walking skeleton we work with the
        # state snapshots already exposed.
        results = await self._gather_all()

        snapshot = ActivitySnapshot(
            entity_states=results.get("entity_states"),
            anthem_status=results.get("anthem_status"),
            lights=results.get("lights"),
            fetched_at=self._clock(),
            partial_errors=results.get("errors", []),
        )

        if (
            snapshot.entity_states is None
            and snapshot.anthem_status is None
            and snapshot.lights is None
        ):
            # Total outage. Log once.
            if not self._unreachable_warned:
                log.warning(
                    "cozylobe: cozyhem-engine unreachable at %s, "
                    "periodic snapshot going quiet on link-loss: %s",
                    self._base_url,
                    "; ".join(snapshot.partial_errors) or "no detail",
                )
                self._unreachable_warned = True
            return None

        if self._unreachable_warned:
            log.info("cozylobe: cozyhem-engine recovered at %s", self._base_url)
            self._unreachable_warned = False
        return snapshot

    async def _gather_all(self) -> dict[str, Any]:
        """Hit all three endpoints sharing one client. Each sub-fetch
        is wrapped so a single endpoint failure doesn't poison the
        rest of the snapshot.
        """
        client_cm = self._http_client_factory()
        results: dict[str, Any] = {"errors": []}
        async with client_cm as client:
            await self._safe_fetch(
                client,
                "/api/v1/entities/states",
                "entity_states",
                results,
            )
            await self._safe_fetch(
                client,
                "/api/v1/anthem/status",
                "anthem_status",
                results,
            )
            await self._safe_fetch(
                client,
                "/api/v1/lights/",
                "lights",
                results,
            )
        return results

    async def _safe_fetch(
        self,
        client: Any,
        path: str,
        key: str,
        results: dict[str, Any],
    ) -> None:
        """One endpoint fetch, wrapped so any failure is recorded but
        does not crash the gather. The wake loop's periodic tick will
        decide whether the partial snapshot is still useful.
        """
        url = f"{self._base_url}{path}"
        try:
            response = await client.get(url)
            response.raise_for_status()
            results[key] = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            results["errors"].append(f"{key}: {type(exc).__name__}: {exc}")
            results[key] = None


# Tiny helper so the wake loop can type-hint its dependency without
# importing httpx at the top of wake_loop.py.
FetchActivity = Callable[[], Awaitable[Optional[ActivitySnapshot]]]
