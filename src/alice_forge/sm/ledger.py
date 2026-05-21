"""Unified emitted-side-effect ledger for SM v3.

Replaces v1's eight special-purpose lists in
``DispatcherState`` (``hello_commented``, ``verify_failed_posted``,
``needs_study_hinted``, ``design_revisions``, ``rebase_attempted``,
``rebase_escalated_posted``, ``exit_required_posted``,
``triage_surfaced``) with a single typed schema.

Every side-effect the dispatcher emits — a comment, a spawn, a
surface file, a label edit — gets one :class:`EmittedRecord`. The
record tracks emission time, TTL (or completion-marker requirement),
and clearance state. New side-effect categories don't need schema
changes; they pick a unique ``side_effect`` name and a default TTL.

Persistence is JSON-on-disk. The file format is schema-versioned;
v1 state files migrate forward on load via :func:`load_or_migrate`
so a Phase 1 cutover doesn't lose dedup history.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class EmittedRecord:
    """One side-effect emission tracked by the dispatcher.

    ``issue_number`` — the GitHub issue this emission attaches to.

    ``side_effect`` — a stable identifier for the *kind* of emission
    (``"hello"``, ``"spawn-started"``, ``"study-hint"``,
    ``"triage-surface"``, ``"parse-error-reply"``, ...). Handlers
    pick the name; the ledger doesn't enforce a fixed vocabulary so
    new effects can be added without schema bumps.

    ``emitted_at`` — UTC timestamp of the emission. Used for TTL
    expiry computation.

    ``ttl_seconds`` — wall-clock budget. ``None`` means the record
    requires an explicit completion marker (``cleared_at`` is set
    when the marker arrives). When non-``None``, the dispatcher's
    sweep pass clears the record after the budget expires AND
    optionally produces a surface so Speaking knows a side-effect
    timed out without resolution.

    ``cleared_at`` — UTC timestamp the record was cleared. ``None``
    while still in flight.

    ``cleared_by`` — provenance string: ``"completion-marker"`` /
    ``"ttl-expiry"`` / ``"manual"`` / ``"replaced"``.

    ``metadata`` — free-form bag for side-effect-specific data
    (spawn dir path, surface filename, prior-state-for-unblock,
    revision counter for the design lane). Schema is by convention,
    not by enforcement — handlers document what they store.
    """

    issue_number: int
    side_effect: str
    emitted_at: _dt.datetime
    ttl_seconds: int | None
    cleared_at: _dt.datetime | None = None
    cleared_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self, now: _dt.datetime) -> bool:
        """True iff the record is still in flight at ``now``.

        Cleared records are inactive. Records with a TTL that has
        expired by ``now`` are also inactive (the sweep would have
        cleared them on the next pass; ``is_active`` returns False
        in advance so a missed sweep doesn't double-emit).
        """
        if self.cleared_at is not None:
            return False
        if self.ttl_seconds is None:
            return True  # awaiting completion marker; stays active forever
        deadline = self.emitted_at + _dt.timedelta(seconds=self.ttl_seconds)
        return now < deadline

    def is_expired(self, now: _dt.datetime) -> bool:
        """True iff the record has a TTL and that TTL has passed at ``now``.

        Used by the sweep pass to drive cleanup and emit-expired
        surfaces. Records with ``ttl_seconds is None`` never expire
        — they require an explicit completion marker.
        """
        if self.cleared_at is not None:
            return False
        if self.ttl_seconds is None:
            return False
        deadline = self.emitted_at + _dt.timedelta(seconds=self.ttl_seconds)
        return now >= deadline


@dataclass
class EmittedLedger:
    """In-memory view of the dispatcher's emit ledger.

    ``records`` is a flat list — small enough (hundreds of entries at
    steady state) that linear scan is fine. If this grows in
    practice, swap the storage to a (issue, side_effect)-keyed dict
    without changing the public API.
    """

    version: int = SCHEMA_VERSION
    records: list[EmittedRecord] = field(default_factory=list)

    def find(
        self, issue_number: int, side_effect: str
    ) -> EmittedRecord | None:
        """Return the most recent record for ``(issue, side_effect)``,
        or ``None``.

        Most callers care about "is this side-effect already in
        flight on this issue" — use :meth:`is_emitted_active` for
        that. :meth:`find` is the raw lookup for callers that want
        metadata or to inspect a cleared record.
        """
        match = None
        for rec in self.records:
            if rec.issue_number == issue_number and rec.side_effect == side_effect:
                if match is None or rec.emitted_at > match.emitted_at:
                    match = rec
        return match

    def is_emitted_active(
        self,
        issue_number: int,
        side_effect: str,
        now: _dt.datetime,
    ) -> bool:
        """True iff there's an active record for ``(issue, side_effect)``."""
        rec = self.find(issue_number, side_effect)
        if rec is None:
            return False
        return rec.is_active(now)

    def mark_emitted(
        self,
        issue_number: int,
        side_effect: str,
        emitted_at: _dt.datetime,
        ttl_seconds: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> EmittedRecord:
        """Record a fresh side-effect emission.

        If a prior record for the same ``(issue, side_effect)``
        exists and is still active, the new emission replaces it
        (marks the old ``cleared_by="replaced"``). This is the right
        semantic for retries that legitimately re-emit (e.g., a
        worker that crashes and is re-spawned).
        """
        prior = self.find(issue_number, side_effect)
        if prior is not None and prior.cleared_at is None:
            prior.cleared_at = emitted_at
            prior.cleared_by = "replaced"
        rec = EmittedRecord(
            issue_number=issue_number,
            side_effect=side_effect,
            emitted_at=emitted_at,
            ttl_seconds=ttl_seconds,
            metadata=dict(metadata or {}),
        )
        self.records.append(rec)
        return rec

    def clear_emitted(
        self,
        issue_number: int,
        side_effect: str,
        cleared_at: _dt.datetime,
        cleared_by: str = "completion-marker",
    ) -> bool:
        """Mark the active record for ``(issue, side_effect)`` cleared.

        Returns ``True`` if a record was found and cleared, ``False``
        if there was nothing active to clear (e.g., the completion
        marker arrived without a prior emit, which is itself a
        signal worth logging).
        """
        rec = self.find(issue_number, side_effect)
        if rec is None or rec.cleared_at is not None:
            return False
        rec.cleared_at = cleared_at
        rec.cleared_by = cleared_by
        return True

    def sweep_expired(
        self, now: _dt.datetime
    ) -> list[EmittedRecord]:
        """Clear all records whose TTL has elapsed by ``now``.

        Returns the list of records swept this pass so the caller
        can emit surfaces / blocks for each. Idempotent: a second
        call with the same ``now`` returns an empty list because
        the swept records now have ``cleared_at`` set.
        """
        swept: list[EmittedRecord] = []
        for rec in self.records:
            if rec.is_expired(now):
                rec.cleared_at = now
                rec.cleared_by = "ttl-expiry"
                swept.append(rec)
        return swept

    # ------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation for on-disk persistence."""
        return {
            "version": self.version,
            "records": [_record_to_dict(rec) for rec in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmittedLedger":
        """Inverse of :meth:`to_dict`. Raises ``ValueError`` on
        version mismatch — caller decides whether to migrate or
        bail."""
        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"emit-ledger schema mismatch: file is v{version!r}, "
                f"expected v{SCHEMA_VERSION}"
            )
        records = [_record_from_dict(d) for d in data.get("records", [])]
        return cls(version=SCHEMA_VERSION, records=records)

    def save(self, path: pathlib.Path) -> None:
        """Atomic write to ``path`` via a temp file + rename.

        Prevents partial writes from corrupting the ledger if the
        process dies mid-write.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)
        )
        tmp.replace(path)


def load_ledger(path: pathlib.Path) -> EmittedLedger:
    """Load an :class:`EmittedLedger` from ``path``.

    Missing file → fresh empty ledger. Corrupt or version-mismatched
    file → raise so the operator notices; we do NOT silently
    overwrite a state file that doesn't parse.
    """
    if not path.exists():
        return EmittedLedger()
    raw = json.loads(path.read_text())
    return EmittedLedger.from_dict(raw)


def load_or_migrate(
    sm_v3_path: pathlib.Path,
    v1_state_path: pathlib.Path | None = None,
) -> EmittedLedger:
    """Phase 1 cutover helper: load v3 ledger; fall back to migrating
    v1 state if v3 file doesn't exist yet.

    Migration mapping (v1 list field → v3 side_effect name):

      * ``hello_commented`` → ``hello``
      * ``verify_failed_posted`` → ``verify-failed``
      * ``needs_study_hinted`` → ``study-hint``
      * ``rebase_attempted`` → ``rebase-attempted``
      * ``rebase_escalated_posted`` → ``rebase-escalated``
      * ``exit_required_posted`` → ``exit-required``
      * ``triage_surfaced`` → ``triage-surface``

    The ``design_revisions`` dict (issue → count) is migrated as
    metadata on a ``design-revision`` record per issue.

    Each migrated entry gets ``emitted_at = now`` (we don't know
    when v1 wrote it) and ``ttl_seconds = None`` (no expiry on the
    migrated records — keeps the dedup behavior identical to v1).
    """
    if sm_v3_path.exists():
        return load_ledger(sm_v3_path)
    if v1_state_path is None or not v1_state_path.exists():
        return EmittedLedger()
    return _migrate_v1_state(v1_state_path)


def _migrate_v1_state(v1_state_path: pathlib.Path) -> EmittedLedger:
    """Translate v1's ``DispatcherState`` JSON into v3 records.

    Best-effort: any v1 field not in the mapping table is dropped
    silently. The v1 state file stays on disk so a rollback to v1
    still has the dedup history.
    """
    raw = json.loads(v1_state_path.read_text())
    ledger = EmittedLedger()
    now = _dt.datetime.now(_dt.timezone.utc)
    flat_mapping = {
        "hello_commented": "hello",
        "verify_failed_posted": "verify-failed",
        "needs_study_hinted": "study-hint",
        "rebase_attempted": "rebase-attempted",
        "rebase_escalated_posted": "rebase-escalated",
        "exit_required_posted": "exit-required",
        "triage_surfaced": "triage-surface",
    }
    for v1_field, v3_name in flat_mapping.items():
        for issue_number in raw.get(v1_field, []) or []:
            if not isinstance(issue_number, int):
                continue
            ledger.mark_emitted(
                issue_number=issue_number,
                side_effect=v3_name,
                emitted_at=now,
                ttl_seconds=None,
                metadata={"migrated_from": v1_field},
            )
    # design_revisions: dict[int, int]
    design_revs = raw.get("design_revisions") or {}
    if isinstance(design_revs, dict):
        for k, v in design_revs.items():
            try:
                issue_number = int(k)
                count = int(v)
            except (TypeError, ValueError):
                continue
            ledger.mark_emitted(
                issue_number=issue_number,
                side_effect="design-revision",
                emitted_at=now,
                ttl_seconds=None,
                metadata={
                    "migrated_from": "design_revisions",
                    "count": count,
                },
            )
    return ledger


# ----------------------------------------------------------------
# JSON encode/decode helpers — keep datetime handling in one place.
# ----------------------------------------------------------------


def _record_to_dict(rec: EmittedRecord) -> dict[str, Any]:
    return {
        "issue_number": rec.issue_number,
        "side_effect": rec.side_effect,
        "emitted_at": rec.emitted_at.isoformat(),
        "ttl_seconds": rec.ttl_seconds,
        "cleared_at": rec.cleared_at.isoformat() if rec.cleared_at else None,
        "cleared_by": rec.cleared_by,
        "metadata": rec.metadata,
    }


def _record_from_dict(d: dict[str, Any]) -> EmittedRecord:
    emitted_at = _dt.datetime.fromisoformat(d["emitted_at"])
    cleared_at_raw = d.get("cleared_at")
    cleared_at = (
        _dt.datetime.fromisoformat(cleared_at_raw) if cleared_at_raw else None
    )
    return EmittedRecord(
        issue_number=int(d["issue_number"]),
        side_effect=str(d["side_effect"]),
        emitted_at=emitted_at,
        ttl_seconds=d.get("ttl_seconds"),
        cleared_at=cleared_at,
        cleared_by=d.get("cleared_by"),
        metadata=dict(d.get("metadata") or {}),
    )
