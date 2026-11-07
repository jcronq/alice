"""GitHub rate-limit pre-flight gate + shared backoff state.

Issue #786 M3 + M4. Two concerns, one module:

M3 — **pre-flight gate**. Before a dispatcher fans out N parallel
workers, call :func:`check_rate_limit` (which calls ``gh api rate_limit``
— this endpoint is FREE and does NOT count against any bucket) and
:func:`should_defer` to decide whether to proceed. If the remaining
quota on ``core`` OR ``graphql`` drops below the safety floor
(default 500 each), the dispatcher defers the fan-out — a 6-parallel
worker burst can rack up 100+ calls in a couple of minutes, which is
what tripped GitHub's secondary rate limit on 2026-11-06 (see #786).

M4 — **shared backoff state**. All gh-callers (watcher, dispatcher,
state-mirror, reconciler, status-site pollers, worker fan-out
dispatchers) read and write a single JSON file
(:data:`DEFAULT_BACKOFF_PATH`) so a 429 observed in one process throttles
every other process for the jittered window. Writes are atomic
(temp-file + ``os.rename``) so a concurrent read can never observe a
torn half-written payload.

The schema is deliberately small and append-safe: the three top-level
blocks (``core`` / ``graphql`` / ``backoff``) each carry their own
timestamp, so a client that upgrades the schema with a new block does
not invalidate older writers.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import random
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Env var ``ALICE_GH_RATE_LIMIT_BACKOFF_PATH`` overrides the default
# location — pytest sets this to a per-test tmp path (see
# ``tests/conftest.py``) so test gh-error observations never leak
# into the production backoff file. Production leaves it unset.
#
# Resolved LAZILY on every helper call via :func:`_resolve_path` so
# tests that twiddle the env var mid-session see the new path
# immediately (no module reload needed).


_PATH_SENTINEL = pathlib.Path("__alice_rate_limit_default__")


def _resolve_path(path: pathlib.Path) -> pathlib.Path:
    """Expand the sentinel default into the active backoff path.

    When callers pass :data:`DEFAULT_BACKOFF_PATH` (the sentinel), the
    env var — or the hard-coded ``/state/worker/...`` default — is
    consulted now, not at import time.
    """
    if path is _PATH_SENTINEL or path == _PATH_SENTINEL:
        env_override = os.environ.get("ALICE_GH_RATE_LIMIT_BACKOFF_PATH")
        if env_override:
            return pathlib.Path(env_override)
        return pathlib.Path("/state/worker/gh-rate-limit-backoff.json")
    return path


# Public sentinel. Callers that explicitly want "the current default"
# pass this (or omit the ``path=`` kwarg). An explicit ``pathlib.Path``
# is always honoured as-is.
DEFAULT_BACKOFF_PATH = _PATH_SENTINEL

# Safety floor. If ``core.remaining`` or ``graphql.remaining`` drops
# below this, the dispatcher defers the fan-out rather than racing
# toward zero. 500 leaves headroom for the ~100-call-per-worker burst
# documented in #786's audit.
DEFAULT_MIN_CORE = 500
DEFAULT_MIN_GRAPHQL = 500

# Exponential backoff schedule used when a 429 / secondary-rate-limit
# is observed. First hit waits 60 s + jitter; each subsequent hit
# doubles up to a 15-min ceiling. The jitter window is 25 % of the
# nominal delay — it prevents a thundering-herd re-try spike when
# every caller reads the same ``backoff_until`` and bursts at once.
_BACKOFF_INITIAL_SECONDS = 60.0
_BACKOFF_CEILING_SECONDS = 900.0
_BACKOFF_JITTER_FRACTION = 0.25

# If ``last_updated`` is older than this, the cached rate-limit numbers
# are discarded (every caller re-queries). GH resets happen on a 1-hour
# window; 10 min is tight enough that we never act on a reading that
# might have aged across a reset boundary.
_STALE_SECONDS = 600.0

# Rough worker-cost estimates used by :func:`estimate_worker_cost`.
# Values come from the #786 audit (``gh pr create`` + CI probes + a
# couple of ``gh pr view`` calls per worker during its lifetime).
_WORKER_COST_TABLE: dict[str, dict[str, int]] = {
    "sm-code-worker": {"core": 20, "graphql": 5},
    "sm-thinking-agent": {"core": 10, "graphql": 10},
    "sm-speaking-agent": {"core": 20, "graphql": 10},
    "sm-design-reviewer": {"core": 5, "graphql": 5},
    "sm-rebase-worker": {"core": 5, "graphql": 0},
    "bug-fix-worker": {"core": 15, "graphql": 5},
    "auto-fix-worker": {"core": 20, "graphql": 5},
    # Catch-all for an uncategorised fan-out. Deliberately conservative
    # so an unknown worker kind doesn't understate the budget.
    "unknown": {"core": 25, "graphql": 10},
}


# ---------------------------------------------------------------------------
# Datatypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitStatus:
    """Snapshot of GitHub's reported rate-limit state.

    All four fields correspond to the keys returned by
    ``gh api rate_limit --jq .resources``. ``core`` + ``graphql`` are
    the two buckets that matter for the dispatcher; ``search`` and
    ``code_search`` are tracked for observability but the pre-flight
    gate does not block on them (M3 scope).
    """

    core_remaining: int
    core_reset_at: str
    graphql_remaining: int
    graphql_reset_at: str
    # Raw payload for callers that want the full picture (search,
    # code_search, integration_manifest, etc.). ``None`` when the
    # status was reconstructed from a cached file rather than a fresh
    # ``gh api`` call.
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackoffState:
    """Snapshot of the shared backoff file.

    ``backoff_until`` is an ISO-8601 UTC string (or ``None`` when no
    backoff is active). Any process that reads a ``backoff_until`` in
    the future must either sleep until it passes or skip the call
    batch entirely.
    """

    last_updated: str
    core_remaining: int | None
    core_reset_at: str | None
    graphql_remaining: int | None
    graphql_reset_at: str | None
    backoff_until: str | None
    backoff_reason: str | None
    # Number of consecutive 429 observations — used to compute the
    # next exponential delay. Resets to 0 when a clean call lands.
    consecutive_hits: int = 0


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(ts: dt.datetime) -> str:
    """Render a tz-aware datetime as a Z-suffixed ISO-8601 string."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 string into a tz-aware UTC datetime.

    Accepts both the ``Z`` suffix we emit and the ``+00:00`` form
    returned by :func:`datetime.isoformat`. Returns ``None`` on any
    parse failure — callers treat an unparseable timestamp as "stale,
    re-query" rather than crashing.
    """
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------------------
# gh api rate_limit — M3 primitive
# ---------------------------------------------------------------------------


def check_rate_limit(
    *,
    gh_bin: str = "gh",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: int = 10,
) -> RateLimitStatus | None:
    """Return GitHub's current rate-limit state, or ``None`` on failure.

    Calls ``gh api rate_limit --jq .resources``. This endpoint is FREE
    — GitHub explicitly documents that ``/rate_limit`` requests do NOT
    count against the primary rate limit (and the response headers
    confirm it). That makes it safe to call every cadence without
    worrying about recursion ("I need the limit to decide whether I
    can ask the limit").

    The ``run`` kwarg lets tests inject a fake subprocess runner.
    """
    try:
        result = run(
            [gh_bin, "api", "rate_limit", "--jq", ".resources"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    stdout = (getattr(result, "stdout", "") or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    core = payload.get("core") or {}
    graphql = payload.get("graphql") or {}
    if not isinstance(core, dict) or not isinstance(graphql, dict):
        return None

    try:
        core_remaining = int(core.get("remaining", 0))
        graphql_remaining = int(graphql.get("remaining", 0))
        core_reset = int(core.get("reset", 0))
        graphql_reset = int(graphql.get("reset", 0))
    except (TypeError, ValueError):
        return None

    return RateLimitStatus(
        core_remaining=core_remaining,
        core_reset_at=_iso(dt.datetime.fromtimestamp(core_reset, tz=dt.timezone.utc)),
        graphql_remaining=graphql_remaining,
        graphql_reset_at=_iso(
            dt.datetime.fromtimestamp(graphql_reset, tz=dt.timezone.utc)
        ),
        raw=payload,
    )


def estimate_worker_cost(worker_kind: str) -> dict[str, int]:
    """Return the rough ``{core, graphql}`` call-cost for a worker kind.

    Values come from the #786 audit. Unknown kinds fall back to the
    ``"unknown"`` row, which is conservative (25 core + 10 graphql) so
    an unclassified worker doesn't understate the budget.
    """
    return dict(_WORKER_COST_TABLE.get(worker_kind, _WORKER_COST_TABLE["unknown"]))


# ---------------------------------------------------------------------------
# Backoff file — M4 primitive
# ---------------------------------------------------------------------------


def _empty_state() -> BackoffState:
    return BackoffState(
        last_updated=_iso(_utcnow()),
        core_remaining=None,
        core_reset_at=None,
        graphql_remaining=None,
        graphql_reset_at=None,
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )


def read_backoff_state(path: pathlib.Path = DEFAULT_BACKOFF_PATH) -> BackoffState:
    """Load the shared backoff state.

    Returns an empty-but-valid :class:`BackoffState` if the file is
    missing, unreadable, or malformed. Callers use the result to gate
    their next gh call batch — a bad file must NOT crash them.
    """
    path = _resolve_path(path)
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return _empty_state()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    core = payload.get("core") if isinstance(payload.get("core"), dict) else {}
    graphql = payload.get("graphql") if isinstance(payload.get("graphql"), dict) else {}
    return BackoffState(
        last_updated=str(payload.get("last_updated") or ""),
        core_remaining=_coerce_int(core.get("remaining")),
        core_reset_at=_coerce_str(core.get("reset_at")),
        graphql_remaining=_coerce_int(graphql.get("remaining")),
        graphql_reset_at=_coerce_str(graphql.get("reset_at")),
        backoff_until=_coerce_str(payload.get("backoff_until")),
        backoff_reason=_coerce_str(payload.get("backoff_reason")),
        consecutive_hits=int(payload.get("consecutive_hits") or 0),
    )


def _coerce_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_str(v: Any) -> str | None:
    if isinstance(v, str) and v:
        return v
    return None


def write_backoff_state(
    state: BackoffState,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
) -> None:
    """Persist ``state`` atomically to ``path``.

    Writes to a sibling temp file and renames into place — a concurrent
    reader can never observe a torn / half-written payload. Parent
    directory is created on demand.
    """
    path = _resolve_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": state.last_updated,
        "core": {
            "remaining": state.core_remaining,
            "reset_at": state.core_reset_at,
        },
        "graphql": {
            "remaining": state.graphql_remaining,
            "reset_at": state.graphql_reset_at,
        },
        "backoff_until": state.backoff_until,
        "backoff_reason": state.backoff_reason,
        "consecutive_hits": state.consecutive_hits,
    }
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def update_rate_limit_snapshot(
    status: RateLimitStatus,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
) -> BackoffState:
    """Merge a fresh ``RateLimitStatus`` into the backoff file.

    Preserves the existing ``backoff_until`` / ``backoff_reason`` —
    seeing a clean ``remaining`` number doesn't by itself clear an
    active backoff window (the clock does that on expiry). Resets
    ``consecutive_hits`` to 0 so the next 429 starts the exponential
    schedule from scratch, since a successful call means the previous
    hit's window has elapsed.
    """
    existing = read_backoff_state(path)
    now = _iso(_utcnow())
    merged = BackoffState(
        last_updated=now,
        core_remaining=status.core_remaining,
        core_reset_at=status.core_reset_at,
        graphql_remaining=status.graphql_remaining,
        graphql_reset_at=status.graphql_reset_at,
        backoff_until=existing.backoff_until,
        backoff_reason=existing.backoff_reason,
        consecutive_hits=0,
    )
    write_backoff_state(merged, path)
    return merged


def record_rate_limit_hit(
    *,
    reason: str,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    now: Callable[[], dt.datetime] = _utcnow,
    jitter: Callable[[float, float], float] = random.uniform,
) -> BackoffState:
    """Mark that a 429 / secondary-rate-limit was observed.

    Bumps ``consecutive_hits``, computes the next exponential delay
    (with jitter), writes ``backoff_until = now + delay``. Idempotent
    in the sense that calling it twice from two concurrent processes
    produces two independent writes but the second one wins (atomic
    rename), both aimed at roughly the same future time — a concurrent
    reader can never observe a torn payload.
    """
    existing = read_backoff_state(path)
    hits = existing.consecutive_hits + 1
    nominal = min(
        _BACKOFF_INITIAL_SECONDS * (2 ** (hits - 1)),
        _BACKOFF_CEILING_SECONDS,
    )
    jittered = nominal * (1.0 + jitter(-_BACKOFF_JITTER_FRACTION, _BACKOFF_JITTER_FRACTION))
    now_dt = now()
    backoff_until = now_dt + dt.timedelta(seconds=jittered)
    next_state = BackoffState(
        last_updated=_iso(now_dt),
        core_remaining=existing.core_remaining,
        core_reset_at=existing.core_reset_at,
        graphql_remaining=existing.graphql_remaining,
        graphql_reset_at=existing.graphql_reset_at,
        backoff_until=_iso(backoff_until),
        backoff_reason=reason,
        consecutive_hits=hits,
    )
    write_backoff_state(next_state, path)
    return next_state


def clear_backoff(
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    *,
    now: Callable[[], dt.datetime] = _utcnow,
) -> BackoffState:
    """Clear the backoff window and reset the consecutive-hit counter.

    Called when a batch of calls returns clean after a prior 429 —
    the next 429 should start its own exponential schedule from 60 s
    rather than jumping to minutes.
    """
    existing = read_backoff_state(path)
    cleared = BackoffState(
        last_updated=_iso(now()),
        core_remaining=existing.core_remaining,
        core_reset_at=existing.core_reset_at,
        graphql_remaining=existing.graphql_remaining,
        graphql_reset_at=existing.graphql_reset_at,
        backoff_until=None,
        backoff_reason=None,
        consecutive_hits=0,
    )
    write_backoff_state(cleared, path)
    return cleared


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeferDecision:
    """Result of :func:`should_defer`.

    ``defer`` is the decision; ``reason`` is a human-facing string the
    caller logs / surfaces to Signal. ``retry_after_s`` is a non-
    negative float the caller can sleep on, or ``None`` when the gate
    is passed.
    """

    defer: bool
    reason: str
    retry_after_s: float | None


def should_defer(
    *,
    min_core: int = DEFAULT_MIN_CORE,
    min_graphql: int = DEFAULT_MIN_GRAPHQL,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    now: Callable[[], dt.datetime] = _utcnow,
    status: RateLimitStatus | None = None,
) -> DeferDecision:
    """Decide whether a fan-out should proceed right now.

    Gate conditions, in order:

      1. ``backoff_until`` in the future → defer; some other process
         (or an earlier call from this one) observed a 429.
      2. Cached ``remaining`` numbers are fresh AND below the floor →
         defer until ``reset_at``.
      3. Cached numbers are stale (``last_updated`` older than 10 min)
         AND no fresh ``status`` was passed in → defer and signal the
         caller to re-query via :func:`check_rate_limit` first.
      4. Otherwise → proceed.

    Callers that have already called :func:`check_rate_limit` can pass
    the result as ``status`` to short-circuit the staleness check.
    """
    now_dt = now()
    state = read_backoff_state(path)

    # (1) Active backoff window.
    backoff_dt = _parse_iso(state.backoff_until)
    if backoff_dt is not None and backoff_dt > now_dt:
        wait = (backoff_dt - now_dt).total_seconds()
        return DeferDecision(
            defer=True,
            reason=(
                f"backoff active until {state.backoff_until} "
                f"(reason={state.backoff_reason or 'unspecified'})"
            ),
            retry_after_s=max(wait, 0.0),
        )

    # Prefer a freshly-observed status over cached numbers.
    if status is not None:
        core_remaining = status.core_remaining
        core_reset = status.core_reset_at
        graphql_remaining = status.graphql_remaining
        graphql_reset = status.graphql_reset_at
    else:
        core_remaining = state.core_remaining
        core_reset = state.core_reset_at
        graphql_remaining = state.graphql_remaining
        graphql_reset = state.graphql_reset_at
        last_seen = _parse_iso(state.last_updated) or dt.datetime.fromtimestamp(
            0, tz=dt.timezone.utc
        )
        age = (now_dt - last_seen).total_seconds()

        # (3) Staleness: if we have NO cached numbers at all, fail open
        # — a missing / brand-new backoff file is the normal shape on a
        # fresh deploy, and the caller should proceed rather than
        # deadlock waiting for a never-arriving snapshot. The dispatcher
        # queries ``check_rate_limit`` explicitly at cadence start, so
        # the "no data" window is short in practice. We only defer when
        # we have stale-but-present numbers AND they'd fail the floor
        # (checked below) — numbers past their reset window are no
        # evidence of throttling.
        if core_remaining is None or graphql_remaining is None:
            return DeferDecision(
                defer=False,
                reason="no cached rate-limit snapshot — proceeding",
                retry_after_s=None,
            )
        # Stale snapshot but numbers are present: if the reset window
        # has already elapsed, those numbers are no longer evidence of
        # a low quota — the bucket has refilled. Proceed.
        if age > _STALE_SECONDS:
            return DeferDecision(
                defer=False,
                reason=(
                    f"cached rate-limit state stale (age={age:.0f}s) "
                    f"— proceeding, caller should re-query"
                ),
                retry_after_s=None,
            )

    # (2) Below the floor on either bucket.
    if core_remaining is not None and core_remaining < min_core:
        reset = _parse_iso(core_reset)
        wait = (
            max((reset - now_dt).total_seconds(), 0.0)
            if reset is not None
            else 60.0
        )
        return DeferDecision(
            defer=True,
            reason=(
                f"core.remaining={core_remaining} below floor "
                f"{min_core} — reset at {core_reset}"
            ),
            retry_after_s=wait,
        )
    if graphql_remaining is not None and graphql_remaining < min_graphql:
        reset = _parse_iso(graphql_reset)
        wait = (
            max((reset - now_dt).total_seconds(), 0.0)
            if reset is not None
            else 60.0
        )
        return DeferDecision(
            defer=True,
            reason=(
                f"graphql.remaining={graphql_remaining} below floor "
                f"{min_graphql} — reset at {graphql_reset}"
            ),
            retry_after_s=wait,
        )

    return DeferDecision(defer=False, reason="within budget", retry_after_s=None)


def wait_until_available(
    *,
    min_core: int = DEFAULT_MIN_CORE,
    min_graphql: int = DEFAULT_MIN_GRAPHQL,
    timeout_s: float = 300.0,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], dt.datetime] = _utcnow,
    gh_bin: str = "gh",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    poll_interval_s: float = 5.0,
) -> bool:
    """Block until the pre-flight gate passes, or ``timeout_s`` elapses.

    Returns ``True`` on gate-pass, ``False`` on timeout. Callers that
    can't afford to block should use :func:`should_defer` directly
    and reschedule themselves.

    A fresh :func:`check_rate_limit` fires on every iteration so the
    loop never sits on a stale snapshot; the fresh status is passed
    into :func:`should_defer` to short-circuit the staleness branch.
    """
    deadline = now() + dt.timedelta(seconds=timeout_s)
    while True:
        status = check_rate_limit(gh_bin=gh_bin, run=run)
        if status is not None:
            update_rate_limit_snapshot(status, path)
        decision = should_defer(
            min_core=min_core,
            min_graphql=min_graphql,
            path=path,
            now=now,
            status=status,
        )
        if not decision.defer:
            return True
        if now() >= deadline:
            return False
        wait = decision.retry_after_s or poll_interval_s
        # Cap the per-iteration sleep so we re-check within a reasonable
        # window even if GitHub reported a multi-minute reset (another
        # process may have cleared the backoff in the meantime).
        sleep(min(wait, poll_interval_s))


# ---------------------------------------------------------------------------
# Public helper used by gh-callers that observe a 429
# ---------------------------------------------------------------------------


def observe_gh_error(
    exc: Exception,
    *,
    path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    now: Callable[[], dt.datetime] = _utcnow,
    jitter: Callable[[float, float], float] = random.uniform,
) -> BackoffState | None:
    """If ``exc`` looks like a rate-limit, record it; else ``None``.

    Accepts anything with a ``looks_like_rate_limit`` property (both
    :class:`alice_forge.dispatcher.errors.GHCommandError` and
    :class:`watchers.github.GHCommandError` qualify). Returns the new
    :class:`BackoffState` on a recorded hit, or ``None`` when the
    exception is not a rate-limit.
    """
    is_rate_limit = getattr(exc, "looks_like_rate_limit", False)
    if not is_rate_limit:
        return None
    reason = str(exc)[:200]
    return record_rate_limit_hit(
        reason=reason, path=path, now=now, jitter=jitter
    )
