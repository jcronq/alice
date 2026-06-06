"""Observability ledger — append/query/prune for implicit feedback signals.

Standalone module backed by a single JSON file at
``~/alice-mind/inner/state/observability-ledger.json``. No thalamus relay
or adaptive-consumer dependency: this is a transparent log of
classification / routing / transport outcomes that callers append to from
three instrumentation points (cozylobe note generation, Stage B intake,
Speaking signal intake).

Concurrency model: every mutation takes an exclusive ``fcntl.LOCK_EX`` on
the ledger file for the full read-modify-write window. Reads take the same
exclusive lock — cheaper than reasoning about shared/exclusive interplay
for a file this small.

Resilience:

* If the ledger file is missing, callers see an empty (but well-formed)
  ledger and the next write creates the file.
* If the ledger file is malformed (truncated, partial write), the
  in-memory copy is reset to the empty schema rather than crashing the
  caller. The next write rewrites a clean file.

Design reference: ``cortex-memory/research/2026-06-06-observability-ledger-design.md``.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

LEDGER_PATH = os.path.expanduser("~/alice-mind/inner/state/observability-ledger.json")

SIGNAL_TYPES: list[str] = [
    "cozylobe_classification",
    "stage_b_routing",
    "speaking_transport",
]

# Auto-determine "useful" based on outcome heuristics per signal type.
# Callers can override by passing ``useful=`` explicitly.
_USEFUL_HEURISTICS: dict[str, set[str]] = {
    "cozylobe_classification": {"consumed", "acted_on"},
    "stage_b_routing": {"consumed"},
    "speaking_transport": {"acted_on"},
}

# EDT offset (-04:00). The vault uses this offset throughout; we match it
# so timestamps line up with dailies and surface files without DST drift.
_EDT = timezone(timedelta(hours=-4))


def _now_iso() -> str:
    return datetime.now(_EDT).isoformat()


def _empty_ledger() -> dict[str, Any]:
    return {
        "version": 1,
        "signals": {sig: {"entries": []} for sig in SIGNAL_TYPES},
    }


def _normalise(ledger: dict[str, Any]) -> dict[str, Any]:
    """Ensure the ledger has the expected shape. Missing signal types are
    backfilled with empty entry lists so callers never KeyError."""
    if not isinstance(ledger, dict):
        return _empty_ledger()
    ledger.setdefault("version", 1)
    signals = ledger.setdefault("signals", {})
    if not isinstance(signals, dict):
        ledger["signals"] = signals = {}
    for sig in SIGNAL_TYPES:
        bucket = signals.setdefault(sig, {"entries": []})
        if not isinstance(bucket, dict) or not isinstance(
            bucket.get("entries"), list
        ):
            signals[sig] = {"entries": []}
    return ledger


def _auto_useful(signal_type: str, outcome: str) -> bool:
    return outcome in _USEFUL_HEURISTICS.get(signal_type, set())


def _ensure_ledger_file() -> None:
    """Create an empty ledger file if the path is absent."""
    if os.path.exists(LEDGER_PATH):
        return
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(_empty_ledger(), f, indent=2)
            f.write("\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load_locked(f: Any) -> dict[str, Any]:
    """Read+parse the open ledger file. Returns an empty ledger if the
    file is empty or malformed — never raises."""
    f.seek(0)
    raw = f.read()
    if not raw.strip():
        return _empty_ledger()
    try:
        return _normalise(json.loads(raw))
    except json.JSONDecodeError:
        return _empty_ledger()


def _dump_locked(f: Any, ledger: dict[str, Any]) -> None:
    f.seek(0)
    f.truncate()
    json.dump(ledger, f, indent=2)
    f.write("\n")


def append_signal(
    signal_type: str,
    *,
    input: dict[str, Any],
    outcome: str,
    useful: bool | None = None,
    ts: str | None = None,
) -> None:
    """Append one entry to the ledger.

    Args:
        signal_type: One of :data:`SIGNAL_TYPES`.
        input: Free-form dict of input fields describing what the signal
            saw (e.g. ``{"event_type": "motion", "room": "playroom"}``).
        outcome: Outcome string. The valid set depends on signal_type;
            see the design note. Anything goes at the storage layer.
        useful: If ``None``, derived from the per-signal-type heuristic
            in :data:`_USEFUL_HEURISTICS`. Pass an explicit ``True`` /
            ``False`` to override the heuristic.
        ts: ISO-8601 timestamp. Defaults to ``datetime.now(EDT)``.

    Raises:
        ValueError: if ``signal_type`` is not in :data:`SIGNAL_TYPES`.
    """
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(
            f"Unknown signal_type: {signal_type!r}. Must be one of {SIGNAL_TYPES}"
        )

    if useful is None:
        useful = _auto_useful(signal_type, outcome)

    entry = {
        "ts": ts if ts is not None else _now_iso(),
        "input": input,
        "outcome": outcome,
        "useful": useful,
    }

    _ensure_ledger_file()
    with open(LEDGER_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = _load_locked(f)
            ledger["signals"][signal_type]["entries"].append(entry)
            _dump_locked(f, ledger)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def query_signals(
    signal_type: str | None = None,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Return ledger entries filtered by signal type and/or start time.

    Each returned entry carries an extra ``signal_type`` key so callers
    handling multiple types can tell them apart.

    Args:
        signal_type: If set, restrict to that signal type. Must be in
            :data:`SIGNAL_TYPES`; otherwise raises ``ValueError``.
        since_iso: If set, only return entries with ``ts >= since_iso``.
            Malformed entry timestamps are dropped silently.

    Returns:
        List of entries in their stored order (oldest first).
    """
    if signal_type is not None and signal_type not in SIGNAL_TYPES:
        raise ValueError(
            f"Unknown signal_type: {signal_type!r}. Must be one of {SIGNAL_TYPES}"
        )

    if not os.path.exists(LEDGER_PATH):
        return []

    with open(LEDGER_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = _load_locked(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    since_dt: datetime | None = None
    if since_iso is not None:
        try:
            since_dt = datetime.fromisoformat(since_iso)
        except ValueError as exc:
            raise ValueError(f"since_iso is not ISO-8601: {since_iso!r}") from exc

    types = [signal_type] if signal_type else SIGNAL_TYPES
    out: list[dict[str, Any]] = []
    for sig in types:
        for entry in ledger["signals"].get(sig, {}).get("entries", []):
            if since_dt is not None:
                try:
                    ts = datetime.fromisoformat(entry["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                # Treat naive timestamps as EDT for comparison purposes.
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_EDT)
                if ts < since_dt:
                    continue
            tagged = dict(entry)
            tagged["signal_type"] = sig
            out.append(tagged)
    return out


def prune_entries(max_age_days: int = 30) -> dict[str, int]:
    """Drop entries older than ``max_age_days`` from every signal type.

    Returns a dict mapping each signal type to the number of entries
    removed. Malformed entries (missing/invalid ``ts``) are dropped too
    and counted against their bucket.
    """
    cutoff = datetime.now(_EDT) - timedelta(days=max_age_days)
    removed: dict[str, int] = {sig: 0 for sig in SIGNAL_TYPES}

    if not os.path.exists(LEDGER_PATH):
        return removed

    with open(LEDGER_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            ledger = _load_locked(f)
            for sig in SIGNAL_TYPES:
                entries = ledger["signals"][sig]["entries"]
                kept: list[dict[str, Any]] = []
                for entry in entries:
                    try:
                        ts = datetime.fromisoformat(entry["ts"])
                    except (KeyError, TypeError, ValueError):
                        removed[sig] += 1
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_EDT)
                    if ts >= cutoff:
                        kept.append(entry)
                    else:
                        removed[sig] += 1
                ledger["signals"][sig]["entries"] = kept
            _dump_locked(f, ledger)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    return removed
