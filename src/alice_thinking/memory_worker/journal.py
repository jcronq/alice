"""Write-ahead journal for memory-worker vault mutations.

Per design §6 (Crash recovery: lock + journal), every intended
vault write is recorded as one JSONL line **before** the mutation
runs. On startup the worker replays any entry whose ``status`` is
still ``"pending"``: it dispatches to a per-op verifier that
checks whether the mutation already landed (idempotent) and marks
the entry ``"committed"``.

Phase 1 only ships the journal mechanics + an empty verifier
registry. The actual B/C/D operations are deferred; verifiers for
``"atomize"``, ``"archive"``, ``"dedupe-merge"`` and
``"frontmatter-update"`` are registered as logging stubs so the
replay path is exercised end-to-end without performing real writes.

Journal record schema
---------------------

::

    {
      "journal_id": "<uuid4-hex>",  # idempotency key, also dedupes replays
      "ts":         "2026-06-01T20:58:00Z",
      "op":         "atomize" | "archive" | "dedupe-merge" | "frontmatter-update",
      "source":     "cortex-memory/research/foo.md",
      "targets":    ["cortex-memory/research/foo-part1.md", ...],
      "detail":     {<op-specific payload>},
      "status":     "pending" | "committed" | "skipped"
    }

Status transitions:

* ``"pending"``   — written by :func:`append`, before the operation runs.
* ``"committed"`` — written by :func:`commit` after a successful
  operation **or** by :func:`replay` once a verifier confirms the
  target state matches what the entry intended.
* ``"skipped"``   — written by :func:`replay` when no verifier is
  registered for ``op`` (Phase 1 default for B/C/D ops). The
  scaffold logs and moves on instead of blocking on an op it
  cannot validate.

The journal is **append-only at write time**. State updates
(``pending`` → ``committed`` / ``skipped``) are written as new
records that share the same ``journal_id``; :func:`load` collapses
records to the latest status per id. This keeps the file
crash-safe — a torn write at the end of the journal loses at most
the last in-flight record, not earlier history.
"""

from __future__ import annotations

import json
import logging
import pathlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


logger = logging.getLogger(__name__)


# Allowed ``op`` values. The set is permissive (replay tolerates
# unknown ops by logging + skipping) so a forward-compatible journal
# from a newer worker doesn't crash an older replay path.
KNOWN_OPS = frozenset({"atomize", "archive", "dedupe-merge", "frontmatter-update"})

PENDING = "pending"
COMMITTED = "committed"
SKIPPED = "skipped"


@dataclass
class JournalEntry:
    """One vault-mutation intent.

    The dataclass form is the in-memory representation; the on-disk
    format is JSONL. :meth:`from_dict` / :meth:`to_dict` convert
    between the two, validating only the structural shape — op-level
    semantics (do the targets exist? does the frontmatter match?)
    live in the verifier dispatch, not here.
    """

    journal_id: str
    ts: str
    op: str
    source: str
    targets: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JournalEntry":
        return cls(
            journal_id=str(raw["journal_id"]),
            ts=str(raw.get("ts", "")),
            op=str(raw["op"]),
            source=str(raw.get("source", "")),
            targets=list(raw.get("targets") or []),
            detail=dict(raw.get("detail") or {}),
            status=str(raw.get("status", PENDING)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "ts": self.ts,
            "op": self.op,
            "source": self.source,
            "targets": list(self.targets),
            "detail": dict(self.detail),
            "status": self.status,
        }


# Verifier signature: ``(entry) -> bool``. Returns True if the
# intended vault state is already present (idempotent — replay
# marks the entry committed). Returns False if the state is NOT
# present, in which case Phase 2+ would re-execute the operation;
# the Phase 1 scaffold logs and leaves the entry pending so a
# later worker (with verifiers wired) can resume.
Verifier = Callable[[JournalEntry], bool]


def _stub_verifier(op_name: str) -> Verifier:
    """Build a logging stub for a known op.

    The stub returns ``False`` (not present) so Phase 1 replay
    treats the entry as ``"skipped"`` rather than ``"committed"`` —
    we deliberately don't claim success on an op the scaffold can't
    actually verify. Behavior is overridden in phases 2–4 when the
    real verifiers ship.
    """

    def verify(entry: JournalEntry) -> bool:
        logger.info(
            "memory-worker: journal verifier for op=%s is a Phase 1 stub; "
            "skipping entry journal_id=%s source=%s",
            op_name,
            entry.journal_id,
            entry.source,
        )
        return False

    return verify


# Phase 1 verifier registry — populated with stubs for the known
# ops so the replay path is exercised. Real verifiers replace these
# in phases 2–4. Tests that need to override a verifier do so via
# :func:`register_verifier`.
_VERIFIERS: dict[str, Verifier] = {op: _stub_verifier(op) for op in KNOWN_OPS}


def register_verifier(op: str, verifier: Verifier) -> None:
    """Register or replace the verifier for ``op``.

    Phase 2+ wires real verifiers via this hook. Tests use it to
    inject deterministic verifiers and exercise the replay branches
    without touching the vault.
    """
    _VERIFIERS[op] = verifier


def reset_verifiers_to_phase1_defaults() -> None:
    """Restore the Phase 1 logging-stub registry.

    Test fixtures call this in teardown to avoid leaking
    test-registered verifiers into the next test's state.
    """
    _VERIFIERS.clear()
    for op in KNOWN_OPS:
        _VERIFIERS[op] = _stub_verifier(op)


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision.

    Centralized so :func:`append` and tests don't disagree about
    the format — and so a monkeypatch in tests can pin the clock.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expand(path: pathlib.Path) -> pathlib.Path:
    """Resolve ``~`` so callers can pass the config-file path verbatim."""
    return pathlib.Path(str(path)).expanduser()


def _ensure_parent(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append(
    journal_path: pathlib.Path,
    *,
    op: str,
    source: str,
    targets: Iterable[str] = (),
    detail: Mapping[str, Any] | None = None,
    journal_id: str | None = None,
    status: str = PENDING,
) -> JournalEntry:
    """Append a new entry and return it.

    Callers run this **before** the vault mutation. After the
    mutation succeeds they call :func:`commit` with the returned
    entry's :attr:`journal_id`. If the worker crashes between the
    two, :func:`replay` finds the still-pending entry on the next
    startup.

    The ``journal_id`` defaults to a fresh UUID4 hex string. Tests
    pass an explicit value to make assertions deterministic.
    """
    if op not in KNOWN_OPS:
        # Forward-compat: log but allow. Verifier dispatch will
        # ``"skipped"`` it on replay.
        logger.warning(
            "memory-worker: journal append using unknown op=%r — "
            "replay will skip this entry",
            op,
        )
    entry = JournalEntry(
        journal_id=journal_id or uuid.uuid4().hex,
        ts=_utc_now_iso(),
        op=op,
        source=source,
        targets=list(targets),
        detail=dict(detail or {}),
        status=status,
    )
    path = _expand(journal_path)
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
    return entry


def commit(journal_path: pathlib.Path, journal_id: str) -> None:
    """Mark an existing entry committed.

    Implemented as a status-update record (same ``journal_id``,
    ``status="committed"``) appended to the journal. :func:`load`
    collapses the history to the latest record per id, so the
    in-memory view stays correct.

    Best-effort: a failure to write the commit record is logged
    and swallowed. The mutation already happened; the worst case is
    that the next replay finds a stale ``pending`` record and
    asks the verifier to confirm it. Since verifiers are
    idempotent, this is safe.
    """
    path = _expand(journal_path)
    record = {
        "journal_id": journal_id,
        "ts": _utc_now_iso(),
        "op": "_status",
        "source": "",
        "targets": [],
        "detail": {},
        "status": COMMITTED,
    }
    try:
        _ensure_parent(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning(
            "memory-worker: failed to commit journal entry %s: %s",
            journal_id,
            exc,
        )


def load(journal_path: pathlib.Path) -> list[JournalEntry]:
    """Read the journal and collapse to the latest record per id.

    The on-disk file is append-only history; this function returns
    one entry per ``journal_id``, with ``status`` reflecting the
    most recent state-transition record. Status updates (``op``
    starting with ``_``) carry no ``source`` / ``targets`` of their
    own — they only update the parent entry's status.

    Malformed lines are skipped with a log message. The replay
    contract is that a torn write at end-of-file loses at most the
    in-flight record, not the rest of the journal.
    """
    path = _expand(journal_path)
    if not path.is_file():
        return []
    entries: dict[str, JournalEntry] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("memory-worker: failed to read journal %s: %s", path, exc)
        return []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            blob = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "memory-worker: skipping malformed journal line %d: %s",
                line_no,
                exc,
            )
            continue
        if not isinstance(blob, dict) or "journal_id" not in blob:
            logger.warning(
                "memory-worker: skipping journal line %d — missing journal_id",
                line_no,
            )
            continue
        jid = str(blob["journal_id"])
        # Status update records carry the new status only; merge
        # into the existing entry if we've seen it, otherwise skip
        # (a status-update for an unknown id has nothing to update).
        if blob.get("op") == "_status":
            if jid in entries:
                entries[jid].status = str(blob.get("status", entries[jid].status))
            continue
        entries[jid] = JournalEntry.from_dict(blob)
    return list(entries.values())


@dataclass
class ReplayReport:
    """Summary of one :func:`replay` pass — useful for telemetry + tests."""

    inspected: int = 0
    already_done: int = 0  # ``status != PENDING`` on disk
    committed: int = 0  # verifier said yes
    skipped: int = 0  # no verifier OR verifier said no
    unknown_op: int = 0  # ``op`` not in registry

    def to_dict(self) -> dict[str, int]:
        return {
            "inspected": self.inspected,
            "already_done": self.already_done,
            "committed": self.committed,
            "skipped": self.skipped,
            "unknown_op": self.unknown_op,
        }


def replay(journal_path: pathlib.Path) -> ReplayReport:
    """Inspect pending entries; commit ones the verifier confirms.

    Called at worker startup before any new operations run. The
    contract is: an entry that's still ``pending`` after replay
    represents a mutation that **may or may not** have completed
    before the prior crash — Phase 2+ verifiers decide, Phase 1
    logs and moves on (``status`` updated to ``"skipped"``).

    Marking ``"skipped"`` rather than leaving ``"pending"`` keeps
    the replay path bounded — without it, every startup would
    re-inspect every historical entry from the beginning of time.
    """
    report = ReplayReport()
    for entry in load(journal_path):
        report.inspected += 1
        if entry.status != PENDING:
            report.already_done += 1
            continue
        verifier = _VERIFIERS.get(entry.op)
        if verifier is None:
            logger.warning(
                "memory-worker: no verifier for op=%s (journal_id=%s); marking skipped",
                entry.op,
                entry.journal_id,
            )
            _write_status(journal_path, entry.journal_id, SKIPPED)
            report.unknown_op += 1
            report.skipped += 1
            continue
        try:
            confirmed = bool(verifier(entry))
        except Exception as exc:  # noqa: BLE001 — verifier failures must not crash replay
            logger.warning(
                "memory-worker: verifier for op=%s raised %s "
                "(journal_id=%s); marking skipped",
                entry.op,
                exc,
                entry.journal_id,
            )
            _write_status(journal_path, entry.journal_id, SKIPPED)
            report.skipped += 1
            continue
        if confirmed:
            _write_status(journal_path, entry.journal_id, COMMITTED)
            report.committed += 1
        else:
            _write_status(journal_path, entry.journal_id, SKIPPED)
            report.skipped += 1
    return report


def _write_status(journal_path: pathlib.Path, journal_id: str, status: str) -> None:
    """Append a status-transition record. Internal helper for :func:`replay`."""
    path = _expand(journal_path)
    record = {
        "journal_id": journal_id,
        "ts": _utc_now_iso(),
        "op": "_status",
        "source": "",
        "targets": [],
        "detail": {},
        "status": status,
    }
    try:
        _ensure_parent(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning(
            "memory-worker: failed to write status=%s for %s: %s",
            status,
            journal_id,
            exc,
        )
