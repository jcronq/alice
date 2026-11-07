"""Memory-worker step: ``derive_sm_seed`` (issue #790, Step 1).

Populates the SM v2 substrate tables (``items``, ``item_external_refs``,
``state_machine``, ``transitions``, ``amendments``) from existing
on-disk state. This is the *seed* step — read-only against GitHub and
the file store, idempotent on re-run, side-effect-free for downstream
code paths. No readers flip to the DB until later steps of #790.

Two input sources, both already produced by Alice today:

1. **GitHub mirror** at ``cortex-memory/gh-state/<org>/<repo>-N.md`` —
   thin frontmatter notes written by :mod:`alice_forge.gh_state_mirror`.
   Each file is one ``gh:issue`` or ``gh:pr`` item. The mirror records
   title + ``state: open/closed`` but no labels, so state-machine
   population from mirror-only data falls back to a neutral
   ``sm:unknown`` seed state; a caller that passes a ``gh_fetcher``
   callable (used in production + tests) gets the real labels and
   comment thread.

2. **Local task store** at ``inner/tasks/task-NNNN/task.yaml`` +
   ``transitions.jsonl``. Each directory is one ``local:task`` item.
   ``task.yaml.status`` is the current state; ``transitions.jsonl``
   is already structured history — one JSON line per transition.

The derive is incremental in the sense that re-running with no
filesystem changes produces zero new rows (upserts match existing
PKs; transitions/amendments dedup on
``(item_id, source, source_ref)``). It is NOT mtime-gated the way
``derive_tasks_workers_events`` is — SM state is small enough that a
full re-walk on every cadence is cheap.

Return shape: :class:`SmSeedReport` mirrors :class:`DeriveReport` so
the memory-worker heartbeat can flatten it with a ``sm_seed_``
prefix.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import pathlib
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from .schema import apply_schema

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass — carried in the memory-worker heartbeat.


@dataclasses.dataclass
class SmSeedReport:
    """Counts + error list from one :func:`derive_sm_seed` pass.

    ``errors`` is a list of ``{"kind": ..., "path": ..., "reason": ...}``
    dicts, same shape as :class:`alice_state_db.derive.DeriveReport`
    so a downstream aggregator can treat the two interchangeably.
    """

    items_upserted: int = 0
    refs_upserted: int = 0
    states_upserted: int = 0
    transitions_inserted: int = 0
    amendments_inserted: int = 0
    gh_items_seen: int = 0
    local_items_seen: int = 0
    gh_fetch_calls: int = 0
    gh_fetch_failures: int = 0
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "items_upserted": self.items_upserted,
            "refs_upserted": self.refs_upserted,
            "states_upserted": self.states_upserted,
            "transitions_inserted": self.transitions_inserted,
            "amendments_inserted": self.amendments_inserted,
            "gh_items_seen": self.gh_items_seen,
            "local_items_seen": self.local_items_seen,
            "gh_fetch_calls": self.gh_fetch_calls,
            "gh_fetch_failures": self.gh_fetch_failures,
            "error_count": len(self.errors),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# Item-id helpers — the human-readable surrogate keys.


def gh_issue_id(repo: str, number: int) -> str:
    """Canonical item_id for a GH issue: ``gh:<repo>#<N>``."""
    return f"gh:{repo}#{number}"


def gh_pr_id(repo: str, number: int) -> str:
    return f"gh:pr:{repo}#{number}"


def local_task_id(task_id: str) -> str:
    """Canonical item_id for a local task directory (``local:task-NNNN``)."""
    return f"local:{task_id}"


# ---------------------------------------------------------------------------
# SM label parsing. ``sm:*`` labels enumerated in
# ``src/alice_forge/sm/states.py``; we keep the list local so this
# module stays free of the dispatcher's import graph (which drags in
# argparse / httpx).


_VALID_SM_STATES = frozenset(
    {
        "draft",
        "needs_study",
        "selected",
        "designing",
        "design_review",
        "designed",
        "compacting",
        "building",
        "reviewing",
        "done",
        "rejected",
        "blocked",
    }
)


def _pick_sm_label(labels: list[str]) -> Optional[str]:
    """Return the ``sm:*`` state (without prefix) from a labels list.

    If multiple ``sm:*`` labels are present (invariant violation — see
    #790 §"Where GH-as-substrate is weakest"), the earliest in the
    declared order wins; the seeder logs it but does not raise so one
    malformed issue cannot wedge the whole derive.
    """
    found: list[str] = []
    for label in labels or []:
        if not isinstance(label, str):
            continue
        if label.startswith("sm:"):
            tail = label[3:]
            if tail in _VALID_SM_STATES:
                found.append(tail)
    if not found:
        return None
    return found[0]


# ``[SM] transition from=X to=Y reason=...`` — matches the audit
# comments written by :mod:`alice_forge.sm.apply`. Reason captures the
# rest of the line, including any trailing ``actor=...`` tokens, so
# the raw audit payload is preserved verbatim.
_SM_TRANSITION_RE = re.compile(
    r"\[SM\]\s+(?:transition\s+)?from=(\S+)\s+to=(\S+)(?:\s+(.*))?",
    re.IGNORECASE,
)

# ``[SM] <verb>`` for non-transition protocol comments (select,
# reject, design-ready, etc.). We don't drive transitions off these
# in Step 1 — the mapping requires the dispatcher's state graph —
# but we DO record them as ``amendments`` of ``source=gh-comment``
# with ``source_ref=<comment id>`` so Step 2's dual-write has the
# audit trail to reconcile against.
_SM_VERB_RE = re.compile(r"^\s*\[SM\]\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# GH mirror parser.


def _parse_mirror_frontmatter(path: pathlib.Path) -> Optional[dict[str, Any]]:
    """Pull the YAML-ish frontmatter block out of a gh-state note.

    The mirror files are hand-shaped by ``gh_state_mirror.write_note_atomic``
    rather than going through a full YAML emitter, so we do a line-wise
    parse rather than depending on pyyaml here. The known keys are:
    ``slug``, ``title``, ``tags``, ``note_type``, ``repo``, ``number``,
    ``type``, ``state``, ``merged``, ``draft``, ``base_branch``,
    ``updated_at``. Unknown keys are preserved so later steps can read
    them.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not text.startswith("---"):
        return None
    try:
        _, body, _ = text.split("---", 2)
    except ValueError:
        return None

    out: dict[str, Any] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key] = value
    return out


def _iter_gh_mirror_files(mind: pathlib.Path) -> Iterator[pathlib.Path]:
    """Yield every ``cortex-memory/gh-state/<org>/<repo>-<N>.md`` file."""
    root = mind / "cortex-memory" / "gh-state"
    if not root.is_dir():
        return
    for org_dir in sorted(root.iterdir()):
        if not org_dir.is_dir():
            continue
        for note in sorted(org_dir.glob("*.md")):
            if note.is_file():
                yield note


# ---------------------------------------------------------------------------
# Local task walkers.


_GH_REF_RE = re.compile(r"\b([\w.\-]+/[\w.\-]+)#(\d+)\b")
_ISSUE_TAG_RE = re.compile(
    r"^github-issue:([\w.\-]+/[\w.\-]+)#(\d+)$", re.IGNORECASE
)
_SHORT_ISSUE_RE = re.compile(r"\b#(\d+)\b")
_REPO_TAG_RE = re.compile(r"^([\w.\-]+/[\w.\-]+)$")


def _load_task_yaml(path: pathlib.Path) -> Optional[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml as _yaml

        data = _yaml.safe_load(text)
    except ImportError:  # pragma: no cover
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _iter_transitions_jsonl(
    path: pathlib.Path,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(line_no, parsed_row)`` from a transitions.jsonl file.

    ``line_no`` is 1-indexed and used as the seeder's ``source_ref`` so
    repeated derive calls dedup cleanly. Malformed lines are skipped
    silently — the SM audit trail is best-effort data, not a hard
    schema.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            for idx, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield idx, row
    except OSError:
        return


def _external_refs_from_task(
    task_id: str, task_data: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Extract ``(ref_type, ref_target_item_id, ref_source)`` triples.

    Reads the task's ``source`` field + every entry in ``tags`` to
    find GH issue cross-references. Ref kinds emitted:

    * ``links_to`` + ``ref_source="task-source"`` when ``source`` is
      ``jcronq/alice#784``-shaped.
    * ``links_to`` + ``ref_source="yaml-tag"`` for a
      ``github-issue:jcronq/alice#784`` tag.
    * ``links_to`` + ``ref_source="bare-hash"`` when a tag like
      ``jcronq/alice`` pairs with a ``#NNN`` in ``source``.

    The dedup is centralised at the caller — same ``(item_id,
    ref_type, ref_target_item_id)`` composite PK makes repeated inserts
    a no-op via ``INSERT OR IGNORE``.
    """
    refs: list[tuple[str, str, str]] = []

    source = task_data.get("source")
    if isinstance(source, str):
        m = _GH_REF_RE.search(source)
        if m:
            refs.append(
                (
                    "links_to",
                    gh_issue_id(m.group(1), int(m.group(2))),
                    "task-source",
                )
            )

    tags_raw = task_data.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [t for t in tags_raw if isinstance(t, str)]

    for tag in tags:
        m = _ISSUE_TAG_RE.match(tag)
        if m:
            refs.append(
                (
                    "links_to",
                    gh_issue_id(m.group(1), int(m.group(2))),
                    "yaml-tag",
                )
            )

    # Bare repo tag + ``#NNN`` in source — covers older tasks that
    # tag ``jcronq/alice`` without a ``github-issue:`` prefix.
    bare_repo: Optional[str] = None
    for tag in tags:
        if _REPO_TAG_RE.match(tag) and "github-issue:" not in tag.lower():
            bare_repo = tag
            break
    if bare_repo and isinstance(source, str) and not _GH_REF_RE.search(source):
        m = _SHORT_ISSUE_RE.search(source)
        if m:
            refs.append(
                (
                    "links_to",
                    gh_issue_id(bare_repo, int(m.group(1))),
                    "bare-hash",
                )
            )

    # merge_ref — https://github.com/<repo>/pull/<N> → ``gh:pr:...``.
    merge_ref = task_data.get("merge_ref")
    if isinstance(merge_ref, str):
        m = re.search(r"github\.com/([\w.\-]+/[\w.\-]+)/pull/(\d+)", merge_ref)
        if m:
            refs.append(
                (
                    "closed_by",
                    gh_pr_id(m.group(1), int(m.group(2))),
                    "merge-ref",
                )
            )

    # dedup locally (same ref might come from source + tag)
    seen = set()
    unique: list[tuple[str, str, str]] = []
    for row in refs:
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


# ---------------------------------------------------------------------------
# DB upsert helpers.


def _upsert_item(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    item_type: str,
    title: str,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    author: Optional[str] = None,
    summary: Optional[str] = None,
    source_path: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO items (
            item_id, item_type, title, created_at, updated_at,
            author, summary, source_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            item_type = excluded.item_type,
            title = excluded.title,
            created_at = COALESCE(items.created_at, excluded.created_at),
            updated_at = excluded.updated_at,
            author = COALESCE(excluded.author, items.author),
            summary = COALESCE(excluded.summary, items.summary),
            source_path = excluded.source_path
        """,
        (
            item_id,
            item_type,
            title,
            created_at,
            updated_at,
            author,
            summary,
            source_path,
        ),
    )
    report.items_upserted += 1


def _upsert_state(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    state: str,
    entered_at: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO state_machine (item_id, state, entered_at)
        VALUES (?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            state = excluded.state,
            entered_at = COALESCE(excluded.entered_at, state_machine.entered_at)
        """,
        (item_id, state, entered_at),
    )
    report.states_upserted += 1


def _insert_ref(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    ref_type: str,
    ref_target_item_id: str,
    ref_source: Optional[str],
) -> None:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO item_external_refs
            (item_id, ref_type, ref_target_item_id, ref_source, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            item_id,
            ref_type,
            ref_target_item_id,
            ref_source,
            _utc_now_iso(),
        ),
    )
    if cur.rowcount:
        report.refs_upserted += 1


def _insert_transition(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    from_state: Optional[str],
    to_state: str,
    actor: Optional[str],
    reason: Optional[str],
    ts: Optional[str],
    source: str,
    source_ref: Optional[str],
) -> Optional[int]:
    """Insert a transition row, dedup-safe via (item_id, source, source_ref).

    Returns the new ``transition_id`` or ``None`` if the row already
    existed. Caller uses the id to set ``state_machine.last_transition_id``
    when seeding a canonical state alongside.
    """
    try:
        cur = conn.execute(
            """
            INSERT INTO transitions (
                item_id, from_state, to_state, actor, reason,
                ts, source, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                from_state,
                to_state,
                actor,
                reason,
                ts,
                source,
                source_ref,
            ),
        )
    except sqlite3.IntegrityError:
        # Unique index conflict (item_id, source, source_ref) — already
        # seeded. Return the existing row's id so the caller can still
        # wire state_machine.last_transition_id to it.
        row = conn.execute(
            """
            SELECT transition_id FROM transitions
            WHERE item_id = ? AND source = ? AND source_ref = ?
            """,
            (item_id, source, source_ref),
        ).fetchone()
        return int(row[0]) if row else None

    report.transitions_inserted += 1
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def _insert_amendment(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    author: Optional[str],
    body: str,
    ts: Optional[str],
    source: str,
    source_ref: Optional[str],
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO amendments (
                item_id, author, body, ts, source, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, author, body, ts, source, source_ref),
        )
    except sqlite3.IntegrityError:
        return
    report.amendments_inserted += 1


# ---------------------------------------------------------------------------
# Local tasks seeder.


def _seed_local_tasks(
    conn: sqlite3.Connection,
    tasks_root: pathlib.Path,
    report: SmSeedReport,
) -> None:
    """Upsert one ``local:task`` item per ``task-*/task.yaml`` directory."""
    if not tasks_root.is_dir():
        return

    for child in sorted(tasks_root.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith("task-"):
            continue
        yaml_path = child / "task.yaml"
        if not yaml_path.is_file():
            continue

        data = _load_task_yaml(yaml_path)
        if data is None:
            report.errors.append(
                {
                    "kind": "local_task",
                    "path": str(yaml_path),
                    "reason": "task.yaml did not parse to a mapping",
                }
            )
            continue

        task_id = str(data.get("id") or child.name)
        item_id = local_task_id(task_id)
        title = str(data.get("title") or "")
        created = str(data.get("created") or "") or None
        updated = str(data.get("updated") or "") or None
        status = str(data.get("status") or "draft")
        author = str(data.get("actor") or "") or None

        report.local_items_seen += 1
        _upsert_item(
            conn,
            report,
            item_id=item_id,
            item_type="local:task",
            title=title,
            created_at=created,
            updated_at=updated,
            author=author,
            source_path=str(yaml_path),
        )
        _upsert_state(
            conn,
            report,
            item_id=item_id,
            state=status,
            entered_at=updated,
        )

        for ref_type, ref_target, ref_source in _external_refs_from_task(
            task_id, data
        ):
            _insert_ref(
                conn,
                report,
                item_id=item_id,
                ref_type=ref_type,
                ref_target_item_id=ref_target,
                ref_source=ref_source,
            )

        # Transitions — read the sibling transitions.jsonl.
        transitions_path = child / "transitions.jsonl"
        last_tid: Optional[int] = None
        for line_no, row in _iter_transitions_jsonl(transitions_path):
            to_state = row.get("to")
            if not isinstance(to_state, str):
                continue
            new_tid = _insert_transition(
                conn,
                report,
                item_id=item_id,
                from_state=row.get("from") if isinstance(row.get("from"), str) else None,
                to_state=to_state,
                actor=row.get("actor") if isinstance(row.get("actor"), str) else None,
                reason=row.get("reason") if isinstance(row.get("reason"), str) else None,
                ts=row.get("ts") if isinstance(row.get("ts"), str) else None,
                source="task-transitions-jsonl",
                source_ref=f"{task_id}:L{line_no}",
            )
            if new_tid is not None:
                last_tid = new_tid

        if last_tid is not None:
            conn.execute(
                "UPDATE state_machine SET last_transition_id = ? WHERE item_id = ?",
                (last_tid, item_id),
            )


# ---------------------------------------------------------------------------
# GH issue seeder.


def _seed_gh_items(
    conn: sqlite3.Connection,
    mind: pathlib.Path,
    report: SmSeedReport,
    gh_fetcher: Optional[Callable[[str, int], Optional[dict[str, Any]]]],
) -> None:
    """Upsert one item per mirrored GH issue / PR, enrich when a fetcher is set.

    Mirror file alone gives us ``title`` + ``state (open/closed)`` + a
    ``note_type`` that tells us issue vs. PR. For the real SM state we
    need labels, which the mirror doesn't record — ``gh_fetcher`` is
    where the caller plugs in either a real ``gh`` CLI shell-out or a
    test double. A ``None`` fetcher is fine: items + a neutral
    ``sm:unknown`` state get seeded and the DB still gains a row for
    every mirrored issue.
    """
    for note in _iter_gh_mirror_files(mind):
        fm = _parse_mirror_frontmatter(note)
        if not fm:
            report.errors.append(
                {
                    "kind": "gh_mirror",
                    "path": str(note),
                    "reason": "frontmatter did not parse",
                }
            )
            continue
        repo = fm.get("repo")
        number_str = fm.get("number")
        kind = (fm.get("type") or "issue").strip().lower()
        if not repo or not number_str:
            continue
        try:
            number = int(number_str)
        except (TypeError, ValueError):
            continue

        if kind == "pr":
            item_id = gh_pr_id(repo, number)
            item_type = "gh:pr"
        else:
            item_id = gh_issue_id(repo, number)
            item_type = "gh:issue"

        title = (fm.get("title") or "").strip() or f"{repo}#{number}"
        updated = fm.get("updated_at")

        report.gh_items_seen += 1
        _upsert_item(
            conn,
            report,
            item_id=item_id,
            item_type=item_type,
            title=title,
            updated_at=updated,
            source_path=str(note),
        )

        # Mirror alone — seed a neutral state. Live-fetched labels
        # override this below.
        default_state = (
            "done" if (fm.get("state") or "").lower() == "closed" else "sm:unknown"
        )
        _upsert_state(
            conn,
            report,
            item_id=item_id,
            state=default_state,
            entered_at=updated,
        )

        if gh_fetcher is None or item_type == "gh:pr":
            # PRs don't carry sm:* state in Alice's model (issue items
            # own the lifecycle), and without a fetcher we can't
            # enrich further. Leave the row in place.
            continue

        try:
            payload = gh_fetcher(repo, number)
            report.gh_fetch_calls += 1
        except Exception as exc:  # noqa: BLE001 — telemetry-only path
            report.gh_fetch_failures += 1
            report.errors.append(
                {
                    "kind": "gh_fetch",
                    "path": f"{repo}#{number}",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        if payload is None:
            continue

        _apply_gh_payload(
            conn,
            report,
            item_id=item_id,
            payload=payload,
            created_at=fm.get("createdAt"),
            updated_at=updated,
        )


def _apply_gh_payload(
    conn: sqlite3.Connection,
    report: SmSeedReport,
    *,
    item_id: str,
    payload: dict[str, Any],
    created_at: Optional[str],
    updated_at: Optional[str],
) -> None:
    """Fold a ``gh issue view`` payload into the SM tables.

    Expected payload shape (matches ``gh issue view --json``):

    .. code-block:: json

        {
          "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
          "author": {"login": "jcronq"},
          "body": "...",
          "createdAt": "...",
          "updatedAt": "...",
          "comments": [
            {"id": "IC_kwDO...", "author": {"login": "jcronq"},
             "body": "[SM] transition from=draft to=selected",
             "createdAt": "..."}
          ]
        }

    Only the fields we actually consume are required; missing keys
    degrade gracefully (``None`` entries).
    """
    labels_raw = payload.get("labels") or []
    label_names: list[str] = []
    for label in labels_raw:
        if isinstance(label, str):
            label_names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                label_names.append(name)

    sm_state = _pick_sm_label(label_names)
    if sm_state:
        _upsert_state(
            conn,
            report,
            item_id=item_id,
            state=sm_state,
            entered_at=updated_at,
        )

    author_login: Optional[str] = None
    author = payload.get("author")
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str):
            author_login = login

    body = payload.get("body")
    if isinstance(body, str):
        conn.execute(
            """
            UPDATE items SET
                author = COALESCE(?, author),
                summary = COALESCE(items.summary, ?)
            WHERE item_id = ?
            """,
            (author_login, body[:500] if body else None, item_id),
        )

    # Comments → transitions + amendments.
    last_tid: Optional[int] = None
    for comment in payload.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        comment_id = str(
            comment.get("id")
            or comment.get("databaseId")
            or _hash_comment_fallback(comment)
        )
        c_author = None
        c_author_blob = comment.get("author")
        if isinstance(c_author_blob, dict):
            login = c_author_blob.get("login")
            if isinstance(login, str):
                c_author = login
        c_ts = comment.get("createdAt") if isinstance(
            comment.get("createdAt"), str
        ) else None

        m = _SM_TRANSITION_RE.search(body)
        if m:
            tid = _insert_transition(
                conn,
                report,
                item_id=item_id,
                from_state=m.group(1),
                to_state=m.group(2),
                actor=c_author,
                reason=(m.group(3) or "").strip() or None,
                ts=c_ts,
                source="gh-comment",
                source_ref=comment_id,
            )
            if tid is not None:
                last_tid = tid
            continue

        if _SM_VERB_RE.match(body):
            # Non-transition [SM] verb — amendment of a protocol kind.
            _insert_amendment(
                conn,
                report,
                item_id=item_id,
                author=c_author,
                body=body,
                ts=c_ts,
                source="gh-comment",
                source_ref=comment_id,
            )
            continue

        # Non-[SM] comment → additive amendment.
        _insert_amendment(
            conn,
            report,
            item_id=item_id,
            author=c_author,
            body=body,
            ts=c_ts,
            source="gh-comment",
            source_ref=comment_id,
        )

    if last_tid is not None:
        conn.execute(
            "UPDATE state_machine SET last_transition_id = ? WHERE item_id = ?",
            (last_tid, item_id),
        )


def _hash_comment_fallback(comment: dict[str, Any]) -> str:
    """Deterministic fallback source_ref when a comment has no id.

    Older ``gh`` versions omit ``id`` on ``gh issue view --json
    comments`` output; hash the ``(body, createdAt)`` pair so repeated
    runs still dedup.
    """
    author = comment.get("author") or {}
    author_login = (
        author.get("login") if isinstance(author, dict) else str(author)
    )
    blob = "|".join(
        [
            str(comment.get("createdAt") or ""),
            str(author_login or ""),
            str(comment.get("body") or ""),
        ]
    )
    return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Production gh_fetcher (lives here so the test suite can import it
# without pulling the dispatcher).


def _default_gh_fetcher(
    repo: str, number: int
) -> Optional[dict[str, Any]]:  # pragma: no cover — shells out
    """Real GH fetcher via the ``gh`` CLI. Kept behind a feature flag.

    The memory-worker wake is already long (I/O-heavy Stage C/D runs);
    we don't want a per-issue ``gh`` fork on every tick. Callers wire
    this explicitly when they want live enrichment; the default
    :func:`derive_sm_seed` path leaves ``gh_fetcher=None`` and seeds
    from mirror-only data.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "labels,body,author,createdAt,updatedAt,comments",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Public entry point.


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_sm_seed(
    mind: pathlib.Path,
    db_path: Optional[pathlib.Path] = None,
    *,
    gh_fetcher: Optional[
        Callable[[str, int], Optional[dict[str, Any]]]
    ] = None,
) -> SmSeedReport:
    """Seed the SM v2 tables from ``mind``.

    Args:
        mind: Root of the ``~/alice-mind`` tree.
        db_path: Override for ``alice-state.db`` location. Defaults to
            ``<mind>/inner/state/alice-state.db``.
        gh_fetcher: Optional callable
            ``(repo: str, number: int) -> dict | None`` used to enrich
            mirrored GH issues with labels + comments. ``None``
            seeds mirror-only (no state_machine transitions from GH).

    Returns:
        :class:`SmSeedReport` with per-table upsert counts.
    """
    started_at = _utc_now_iso()
    started_mono = time.monotonic()

    mind = pathlib.Path(mind)
    tasks_root = mind / "inner" / "tasks"

    if db_path is None:
        db_path = mind / "inner" / "state" / "alice-state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    report = SmSeedReport()
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)

        _seed_gh_items(conn, mind, report, gh_fetcher)
        _seed_local_tasks(conn, tasks_root, report)

        finished_at = _utc_now_iso()
        report.duration_ms = int((time.monotonic() - started_mono) * 1000)

        conn.execute(
            """
            INSERT INTO derive_runs (
                step, started_at, finished_at, rows_touched,
                tasks_upserted, workers_upserted, events_appended,
                last_event_offset, errors_json
            ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
            """,
            (
                "derive_sm_seed",
                started_at,
                finished_at,
                report.items_upserted
                + report.refs_upserted
                + report.states_upserted
                + report.transitions_inserted
                + report.amendments_inserted,
                json.dumps(report.errors),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return report
