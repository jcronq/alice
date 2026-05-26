"""SM v2 task store — file operations over ``inner/tasks/``.

Issue #375 ships this layer so the SM v2 state machine gets producers
wired to real workflows. The store is a thin wrapper around the on-disk
shape that already exists (task-0001 ... task-0018 written by hand
during the design sessions):

    inner/tasks/
        index.jsonl                 # one line per task (status snapshot)
        task-NNNN/
            task.yaml               # canonical metadata
            transitions.jsonl       # append-only state-change log

The contract:

* **Atomic writes.** ``task.yaml`` is written through a tempfile +
  ``os.rename`` so a crash can't corrupt the canonical record.
* **Append-only locking.** Both ``transitions.jsonl`` and
  ``index.jsonl`` are appended under an ``fcntl.flock`` on the index
  file so concurrent invocations (Speaking + Thinking + ad-hoc CLI)
  can't interleave writes. The index file doubles as the lock —
  one lock for the whole store.
* **Transition validation.** Edges enforced per SM v2 design
  (see ``cortex-memory/research/2026-05-11-idea-task-state-machine-v2.md``).
  The graph is forgiving in two places: legacy state ``review`` is
  accepted on read (task-0001 has it) and ``building → done`` with
  ``--merge-ref`` is permitted as the self-merge shortcut Jason's
  feedback codified ("PRs are a git log of work, not a review queue").

This module is kernel-agnostic. The CLI (``bin/task``) and the
Speaking-side auto-fix wiring both call it; pi-coding-agent will too
once the dispatcher integration ships there.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Optional


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State machine

# Canonical SM v2 states. Order matters only for documentation; the
# set is what gets validated against.
VALID_STATES: frozenset[str] = frozenset(
    {
        "draft",
        "selected",
        "building",
        "reviewing",
        "blocked",
        "validating",
        "done",
        "rejected",
    }
)

# Legacy state ``review`` appears in task-0001's transitions.jsonl —
# from the original v1 design before ``reviewing`` was settled on. We
# accept it on read for compatibility but never write it.
LEGACY_STATES: frozenset[str] = frozenset({"review"})

# Terminal states — no outgoing transitions allowed.
TERMINAL_STATES: frozenset[str] = frozenset({"done", "rejected"})

# Adjacency list of valid transitions per SM v2 design doc.
# ``building → done`` with a ``merge_ref`` is added as the self-merge
# shortcut (see module docstring). The plain ``building → done``
# without a merge_ref is still rejected.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"selected", "rejected"}),
    "selected": frozenset({"building", "rejected"}),
    # ``building → done`` is the self-merge shortcut; handled as a
    # special-case in ``_check_transition`` so the ``--merge-ref``
    # requirement actually fires. Not listed here to keep the
    # ordinary adjacency lookup honest.
    "building": frozenset({"reviewing", "blocked", "rejected"}),
    "reviewing": frozenset({"done", "validating", "building", "rejected"}),
    "blocked": frozenset({"building", "rejected"}),
    "validating": frozenset({"done", "building", "blocked"}),
    # Legacy: tolerate edges out of ``review`` so an in-flight task
    # that pre-dates the rename can still be advanced.
    "review": frozenset({"reviewing", "building", "rejected"}),
    # Terminal states — empty set; transitions out are rejected.
    "done": frozenset(),
    "rejected": frozenset(),
}


# ---------------------------------------------------------------------------
# Errors


class TaskStoreError(Exception):
    """Base class for all task-store failures."""


class TaskNotFound(TaskStoreError):
    """No task directory matching the given id."""


class InvalidTransition(TaskStoreError):
    """Requested ``from → to`` is not a valid SM v2 edge."""


class InvalidState(TaskStoreError):
    """Requested status is not a recognised SM v2 state."""


# ---------------------------------------------------------------------------
# Schema


@dataclass
class TaskRecord:
    """In-memory shape of a task. Mirrors ``task.yaml`` fields.

    Optional fields default to ``None`` so we can round-trip records
    that pre-date a field addition without losing data.
    """

    id: str
    title: str
    status: str
    created: str
    updated: str
    actor: str = "speaking"
    artifact_type: str = "code"
    source: str = "speaking"
    tags: list[str] = field(default_factory=list)
    artifact_ref: Optional[str] = None
    merge_ref: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_yaml_dict(self) -> dict[str, Any]:
        """Render to the dict shape that ``task.yaml`` serialises.

        Optional fields are dropped when ``None`` to keep on-disk
        records minimal — matches task-0001's hand-written shape.
        """
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "status": self.status,
            "actor": self.actor,
            "artifact_type": self.artifact_type,
            "source": self.source,
            "tags": list(self.tags),
        }
        if self.artifact_ref is not None:
            out["artifact_ref"] = self.artifact_ref
        if self.merge_ref is not None:
            out["merge_ref"] = self.merge_ref
        for k, v in self.extra.items():
            out.setdefault(k, v)
        return out


# ---------------------------------------------------------------------------
# Time + ids


def _utc_now_iso() -> str:
    """Return current time as an ISO-8601 string with timezone.

    Format matches task-0001's ``2026-05-11T21:33:00-04:00`` shape
    (offset, not literal ``Z``), but we emit UTC by default since
    Speaking and Thinking can run in different containers. Callers
    can override via ``now=`` for tests.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_TASK_ID_RE = re.compile(r"^task-(\d{4,})$")


def _parse_task_id(task_id: str) -> int:
    """Return the integer suffix of a ``task-NNNN`` id."""
    m = _TASK_ID_RE.match(task_id)
    if not m:
        raise TaskStoreError(f"invalid task id: {task_id!r}")
    return int(m.group(1))


def _format_task_id(n: int) -> str:
    """Format an integer as ``task-NNNN`` (zero-padded to 4)."""
    return f"task-{n:04d}"


# ---------------------------------------------------------------------------
# Store


class TaskStore:
    """File operations over a single ``inner/tasks/`` directory.

    Instantiate once per process — the constructor establishes the
    root path but doesn't open any file handles. Per-call methods
    acquire a flock on ``index.jsonl`` for the duration of any
    write, so multiple ``TaskStore`` instances pointed at the same
    directory are safe (different processes too).
    """

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)

    # ------------------------------------------------------------------
    # Internal helpers

    @property
    def index_path(self) -> pathlib.Path:
        return self.root / "index.jsonl"

    def _task_dir(self, task_id: str) -> pathlib.Path:
        return self.root / task_id

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an ``fcntl.flock`` on ``index.jsonl`` for the duration
        of the ``with`` block.

        Creates the index file if missing so the lock has something
        to bite. Releases on exception.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.touch(exist_ok=True)
        with open(self.index_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write_text(path: pathlib.Path, text: str) -> None:
        """Write ``text`` to ``path`` via tempfile + rename.

        ``os.rename`` within the same directory is atomic on POSIX,
        so a crash leaves either the old file intact or the new file
        complete — never a half-written file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.rename(tmp, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _yaml_dump(data: Mapping[str, Any]) -> str:
        """Serialise a task.yaml dict.

        Uses PyYAML when available (preferred for round-tripping),
        falls back to a hand-rolled emitter that matches task-0001's
        hand-written shape: scalar keys unquoted, strings quoted only
        when they contain colons/special chars, lists inline with
        square brackets, ISO timestamps quoted.
        """
        try:
            import yaml as _yaml
        except ImportError:  # pragma: no cover — yaml is a hard dep in pyproject
            return _yaml_dump_fallback(data)
        return _yaml.safe_dump(
            dict(data), sort_keys=False, default_flow_style=False, allow_unicode=True
        )

    # ------------------------------------------------------------------
    # Read paths

    def load(self, task_id: str) -> TaskRecord:
        """Load a task by id. Raises :class:`TaskNotFound`."""
        yaml_path = self._task_dir(task_id) / "task.yaml"
        if not yaml_path.is_file():
            raise TaskNotFound(task_id)
        text = yaml_path.read_text()
        data = _parse_yaml_lenient(text)
        return _record_from_dict(data)

    def transitions(self, task_id: str) -> list[dict[str, Any]]:
        """Return the parsed transitions.jsonl lines, oldest first."""
        path = self._task_dir(task_id) / "transitions.jsonl"
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
        return out

    def iter_index(self) -> Iterator[dict[str, Any]]:
        """Yield index.jsonl entries oldest first."""
        if not self.index_path.is_file():
            return
        for line in self.index_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

    def list(
        self,
        *,
        status: Optional[str] = None,
        tag: Optional[str] = None,
        actor: Optional[str] = None,
        open_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Filtered task summary read from ``index.jsonl``.

        ``open_only=True`` is sugar for ``status not in {done, rejected}`` —
        the common "what's in flight" query. Other filters AND together.
        """
        out: list[dict[str, Any]] = []
        for entry in self.iter_index():
            if status is not None and entry.get("status") != status:
                continue
            if open_only and entry.get("status") in {"done", "rejected"}:
                continue
            if tag is not None and tag not in (entry.get("tags") or []):
                continue
            if actor is not None and entry.get("actor") != actor:
                continue
            out.append(entry)
        return out

    def find_by_tag(self, tag: str) -> Optional[dict[str, Any]]:
        """Return the most-recently-updated open entry carrying ``tag``.

        Used by the auto-fix dispatcher to look up a task by
        ``<repo>#<N>`` at each transition point. Returns ``None`` if
        no open match exists — the caller decides whether that's a
        no-op or an error.
        """
        candidates = [
            entry
            for entry in self.iter_index()
            if tag in (entry.get("tags") or [])
            and entry.get("status") not in {"done", "rejected"}
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.get("updated", ""), reverse=True)
        return candidates[0]

    # ------------------------------------------------------------------
    # Write paths

    def _next_id(self) -> str:
        """Allocate the next ``task-NNNN`` id by scanning existing dirs.

        Called under the index lock; safe against concurrent ``create``.
        """
        max_n = 0
        if self.root.is_dir():
            for child in self.root.iterdir():
                if not child.is_dir():
                    continue
                m = _TASK_ID_RE.match(child.name)
                if m:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
        return _format_task_id(max_n + 1)

    def create(
        self,
        *,
        title: str,
        actor: str = "speaking",
        artifact_type: str = "code",
        source: str = "speaking",
        tags: Optional[Iterable[str]] = None,
        reason: Optional[str] = None,
        now: Optional[str] = None,
    ) -> TaskRecord:
        """Allocate a new ``task-NNNN/`` with status=draft.

        Writes task.yaml, transitions.jsonl (single ``null → draft``
        line), and appends an index.jsonl entry. Returns the loaded
        :class:`TaskRecord`.
        """
        if actor not in {"speaking", "thinking", "jason", "alice"}:
            raise TaskStoreError(f"invalid actor: {actor!r}")
        ts = now or _utc_now_iso()
        tags_list = sorted(set(tags or []))

        with self._locked():
            task_id = self._next_id()
            record = TaskRecord(
                id=task_id,
                title=title,
                status="draft",
                created=ts,
                updated=ts,
                actor=actor,
                artifact_type=artifact_type,
                source=source,
                tags=tags_list,
            )
            task_dir = self._task_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=False)
            self._atomic_write_text(
                task_dir / "task.yaml", self._yaml_dump(record.to_yaml_dict())
            )
            transition = {
                "ts": ts,
                "from": None,
                "to": "draft",
                "actor": actor,
                "reason": reason or f"Task created by {actor}",
            }
            with open(task_dir / "transitions.jsonl", "a") as fh:
                fh.write(json.dumps(transition) + "\n")
            self._append_index(record)
        return record

    def update(
        self,
        task_id: str,
        *,
        status: str,
        actor: str = "speaking",
        reason: Optional[str] = None,
        merge_ref: Optional[str] = None,
        validation_evidence: Optional[str] = None,
        unblocked_by: Optional[str] = None,
        now: Optional[str] = None,
    ) -> TaskRecord:
        """Transition a task to ``status``.

        Validates the edge against :data:`VALID_TRANSITIONS`. Required
        sidecar fields per SM v2 (``unblocked_by`` for ``→ blocked``,
        ``validation_evidence`` for ``validating → done``) are enforced.

        Appends a transitions.jsonl line, rewrites task.yaml atomically,
        updates the index entry. The whole sequence runs under the
        index lock.
        """
        if status not in VALID_STATES:
            raise InvalidState(
                f"unknown status {status!r}; "
                f"expected one of {sorted(VALID_STATES)}"
            )
        ts = now or _utc_now_iso()
        with self._locked():
            record = self.load(task_id)
            self._check_transition(
                record.status, status, merge_ref=merge_ref
            )
            # Validate sidecar requirements
            if status == "blocked" and not unblocked_by:
                raise TaskStoreError(
                    "→ blocked requires --unblocked-by (SM v2 §Required Transition Fields)"
                )
            if record.status == "validating" and status == "done" and not validation_evidence:
                raise TaskStoreError(
                    "validating → done requires --validation-evidence"
                )

            # Mutate the in-memory record
            record.status = status
            record.updated = ts
            if merge_ref is not None:
                record.merge_ref = merge_ref

            # Persist task.yaml
            self._atomic_write_text(
                self._task_dir(task_id) / "task.yaml",
                self._yaml_dump(record.to_yaml_dict()),
            )

            # Append transitions.jsonl
            transition: dict[str, Any] = {
                "ts": ts,
                "from": self._previous_status(task_id),
                "to": status,
                "actor": actor,
                "reason": reason or f"Transition to {status}",
            }
            if merge_ref is not None:
                transition["merge_ref"] = merge_ref
            if validation_evidence is not None:
                transition["validation_evidence"] = validation_evidence
            if unblocked_by is not None:
                transition["unblocked_by"] = unblocked_by
            with open(
                self._task_dir(task_id) / "transitions.jsonl", "a"
            ) as fh:
                fh.write(json.dumps(transition) + "\n")

            # Refresh index — the simplest correct approach is to
            # re-read and rewrite the file under the lock. The index
            # is small (one line per task) so the rewrite cost is
            # negligible at our scale.
            self._rewrite_index_replacing(record)
        return record

    def close(
        self,
        task_id: str,
        *,
        reason: Optional[str] = None,
        merge_ref: Optional[str] = None,
        actor: str = "speaking",
        now: Optional[str] = None,
    ) -> TaskRecord:
        """Sugar for ``update(status="done")``."""
        return self.update(
            task_id,
            status="done",
            actor=actor,
            reason=reason or "Closed",
            merge_ref=merge_ref,
            now=now,
        )

    # ------------------------------------------------------------------
    # Internal — index + validation

    def _previous_status(self, task_id: str) -> Optional[str]:
        """Return the most recent ``to`` from transitions.jsonl, or
        ``None`` if the only entry was the creation row."""
        trans = self.transitions(task_id)
        if len(trans) < 2:
            # First real transition (the initial null→draft is at idx 0,
            # so the previous status is the status BEFORE the call that
            # just appended). The caller has already validated the edge
            # against the record's pre-update status, so we use that.
            if not trans:
                return None
            return trans[-1].get("to")
        return trans[-1].get("to")

    def _check_transition(
        self, current: str, target: str, *, merge_ref: Optional[str]
    ) -> None:
        """Raise :class:`InvalidTransition` if ``current → target``
        isn't an allowed edge."""
        if current in TERMINAL_STATES:
            raise InvalidTransition(
                f"cannot transition out of terminal state {current!r}"
            )
        # Self-merge shortcut: building → done is allowed when a
        # merge_ref is provided. Captures the "I opened a draft
        # PR and merged it myself" path Jason's feedback codified.
        # Handled before the adjacency lookup so the ``--merge-ref``
        # requirement actually fires (otherwise the edge would
        # silently pass when the kwarg is omitted).
        if current == "building" and target == "done":
            if merge_ref:
                return
            raise InvalidTransition(
                "building → done requires --merge-ref (PR or commit URL)"
            )
        allowed = VALID_TRANSITIONS.get(current)
        if allowed is None:
            raise InvalidTransition(
                f"unknown current state {current!r} — has this task been "
                f"migrated to SM v2 yet?"
            )
        if target not in allowed:
            raise InvalidTransition(
                f"{current!r} → {target!r} is not a valid SM v2 edge; "
                f"allowed from {current!r}: {sorted(allowed)}"
            )

    def _append_index(self, record: TaskRecord) -> None:
        """Append a fresh entry to ``index.jsonl``. Called from
        :meth:`create` under the lock."""
        entry = {
            "id": record.id,
            "title": record.title,
            "status": record.status,
            "created": record.created,
            "updated": record.updated,
            "tags": list(record.tags),
        }
        with open(self.index_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _rewrite_index_replacing(self, record: TaskRecord) -> None:
        """Re-emit index.jsonl with ``record``'s entry refreshed.

        Other entries pass through untouched. If ``record.id`` isn't
        present, append it (defensive — shouldn't happen since
        ``create`` always appends).
        """
        entries: list[dict[str, Any]] = list(self.iter_index())
        new_entry = {
            "id": record.id,
            "title": record.title,
            "status": record.status,
            "created": record.created,
            "updated": record.updated,
            "tags": list(record.tags),
        }
        replaced = False
        for i, entry in enumerate(entries):
            if entry.get("id") == record.id:
                entries[i] = new_entry
                replaced = True
                break
        if not replaced:
            entries.append(new_entry)
        body = "\n".join(json.dumps(e) for e in entries) + "\n"
        self._atomic_write_text(self.index_path, body)


# ---------------------------------------------------------------------------
# YAML helpers


def _record_from_dict(data: Mapping[str, Any]) -> TaskRecord:
    """Build a :class:`TaskRecord` from a parsed task.yaml dict.

    Unknown keys land in ``extra`` so we round-trip them on update.
    """
    known = {
        "id",
        "title",
        "status",
        "created",
        "updated",
        "actor",
        "artifact_type",
        "source",
        "tags",
        "artifact_ref",
        "merge_ref",
    }
    extra = {k: v for k, v in data.items() if k not in known}
    return TaskRecord(
        id=str(data["id"]),
        title=str(data.get("title", "")),
        status=str(data.get("status", "draft")),
        created=str(data.get("created", "")),
        updated=str(data.get("updated", "")),
        actor=str(data.get("actor", "speaking")),
        artifact_type=str(data.get("artifact_type", "code")),
        source=str(data.get("source", "speaking")),
        tags=list(data.get("tags") or []),
        artifact_ref=data.get("artifact_ref"),
        merge_ref=data.get("merge_ref"),
        extra=extra,
    )


def _parse_yaml_lenient(text: str) -> dict[str, Any]:
    """Parse a small YAML document.

    Tries PyYAML first. Falls back to a tiny key:value parser that
    handles the subset used by task-0001 (strings, simple lists,
    quoted scalars). The fallback exists so the CLI can run in
    minimal environments without yaml installed — but the runtime
    always has it (it's a hard dep).
    """
    try:
        import yaml as _yaml

        data = _yaml.safe_load(text)
        if not isinstance(data, dict):
            raise TaskStoreError("task.yaml must be a YAML mapping")
        return data
    except ImportError:  # pragma: no cover
        return _yaml_parse_fallback(text)


def _yaml_parse_fallback(text: str) -> dict[str, Any]:
    """Last-resort YAML parser for environments without pyyaml."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [
                p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()
            ]
            out[key] = items
        else:
            out[key] = value
    return out


def _yaml_dump_fallback(data: Mapping[str, Any]) -> str:  # pragma: no cover
    """Last-resort YAML emitter for environments without pyyaml."""
    lines: list[str] = []
    for k, v in data.items():
        if isinstance(v, list):
            inner = ", ".join(json.dumps(item) for item in v)
            lines.append(f"{k}: [{inner}]")
        elif isinstance(v, str):
            lines.append(f"{k}: {json.dumps(v)}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Default-root resolution


def default_root() -> pathlib.Path:
    """Resolve the default ``inner/tasks/`` directory.

    Priority:

    1. ``TASKS_DIR`` env var.
    2. ``ALICE_MIND_DIR/inner/tasks`` if ``ALICE_MIND_DIR`` is set.
    3. ``~/alice-mind/inner/tasks`` — the canonical container path.

    The CLI surfaces ``--root`` to override at invocation time; this
    function is the fallback when nothing's specified.
    """
    if "TASKS_DIR" in os.environ:
        return pathlib.Path(os.environ["TASKS_DIR"])
    if "ALICE_MIND_DIR" in os.environ:
        return pathlib.Path(os.environ["ALICE_MIND_DIR"]) / "inner" / "tasks"
    return pathlib.Path.home() / "alice-mind" / "inner" / "tasks"
