"""DB-backed readers for the dispatcher's open-issue poll (issue #826).

Phase cutover plan (#823):

* **Shadow (default, 48h):** ``ALICE_DISPATCHER_SHADOW=1``. The
  dispatcher still calls the ``gh issue list --label sm:*`` helpers
  in :mod:`alice_forge.dispatcher.gh` as the authoritative source,
  but ALSO runs the matching DB query. Divergence is logged as a
  structured JSON note under ``~/alice-mind/inner/notes/`` (tag
  ``dispatcher-shadow``) and to stdout. Behaviour is unchanged.
* **Cutover:** ``ALICE_DISPATCHER_SHADOW=0``. The DB result becomes
  authoritative. The GH helper is the fail-open fallback: if the
  DB open fails, log and fall back to the GH query so Alice is
  never locked out of her primary job.

Shadow is observational — this PR does NOT flip the flag. The
rollout is: merge → observe shadow log → confirm diffs settle →
set env var to 0 in the s6 service unit.

Known fidelity gaps (acknowledged, filed as follow-ons):

* ``items.summary`` carries only the first 500 chars of the GH
  issue body — handlers that need full body will degrade in
  cutover mode. For the shadow window this is irrelevant because
  GH stays authoritative.
* The DB does not yet store ``art:*`` / ``bug:*`` labels. The
  synthetic ``labels`` list in :func:`db_list_sm_issues`'s dicts
  carries only the ``sm:<state>`` label. Cutover handlers that
  branch on ``art:*`` lose that signal until a follow-up PR adds
  a labels table.
* The DB does not track GH ``open`` vs ``closed`` status.
  :func:`db_list_stale_closed_sm_issues` returns ``[]`` for now
  so the shadow diff surfaces every entry (expected divergence —
  the cutover PR for that sweep adds the column).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from alice_forge.dispatcher.constants import (
    DONE_SM_LABEL,
    NON_TERMINAL_SM_LABELS,
    RECENT_ISSUE_LIMIT,
    SM_LABEL_WHITELIST,
)
from alice_state_db import DEFAULT_DB_PATH, connect as _connect_state_db


# ---------------------------------------------------------------------------
# Env-gated mode switch
# ---------------------------------------------------------------------------


def shadow_enabled() -> bool:
    """Return True when ``ALICE_DISPATCHER_SHADOW`` is truthy or unset.

    Default is ON (safe rollout) — a missing env var reads as shadow
    mode. Flip to ``0`` / ``false`` / ``no`` / ``off`` to cut over.
    """
    raw = os.environ.get("ALICE_DISPATCHER_SHADOW")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# item_id <-> (repo, number) plumbing
# ---------------------------------------------------------------------------


# ``gh:<org>/<repo>#<N>`` — matches :func:`alice_state_db.gh_issue_id`.
_ITEM_ID_RE = re.compile(r"^gh:([\w.\-]+/[\w.\-]+)#(\d+)$")


def _parse_gh_item_id(item_id: str) -> Optional[tuple[str, int]]:
    """Return ``(repo, number)`` or ``None`` for non-issue items.

    PR items (``gh:pr:<repo>#<N>``) and local tasks (``local:...``)
    deliberately parse as ``None`` — the dispatcher cadence acts on
    GH issues only.
    """
    m = _ITEM_ID_RE.match(item_id)
    if m is None:
        return None
    return m.group(1), int(m.group(2))


# ---------------------------------------------------------------------------
# Row → GH-shaped issue dict
# ---------------------------------------------------------------------------


def _row_to_issue_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Synthesize a GH-shaped issue dict from a state_machine JOIN items row.

    The shape mirrors the output of
    :func:`alice_forge.dispatcher.gh.gh_list_sm_issues` so downstream
    handlers see a type-compatible payload. Fields:

    * ``number``: parsed from ``item_id``.
    * ``title``: from ``items.title``.
    * ``labels``: single-entry list carrying the ``sm:<state>`` label.
      ``art:*`` / ``bug:*`` are not yet stored in the DB — handlers
      that branch on them degrade until a follow-up PR adds a labels
      table.
    * ``author``: dict shape matching GH's ``{login: <handle>}``.
    * ``createdAt``: ``items.created_at`` (camelCase to match GH's
      ``--json createdAt`` output).
    * ``body``: ``items.summary`` (first 500 chars of the real body
      per :mod:`alice_state_db.gh_poll` convention). Cutover users
      who need full body need a follow-up PR; shadow readers never
      touch this.
    """
    parsed = _parse_gh_item_id(row["item_id"])
    if parsed is None:
        return {}
    _, number = parsed
    state_label = row["state"]
    labels: list[dict[str, str]] = [{"name": state_label}]
    author_login = row["author"] if "author" in row.keys() else None
    return {
        "number": number,
        "title": row["title"] if "title" in row.keys() else "",
        "labels": labels,
        "author": {"login": author_login} if author_login else {},
        "createdAt": row["created_at"] if "created_at" in row.keys() else None,
        "body": row["summary"] if "summary" in row.keys() else "",
        # Internal provenance marker so downstream debug can tell a
        # DB-synthesised dict from a real gh payload.
        "_source": "db",
    }


# ---------------------------------------------------------------------------
# DB readers — paired with each gh_list_*_sm_issues helper
# ---------------------------------------------------------------------------


def _list_items_in_states(
    conn: sqlite3.Connection,
    repo: str,
    states: frozenset[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return GH-issue items whose state is in ``states`` for ``repo``.

    Ordered by ``entered_at`` ASC so FIFO fairness matches the
    GH-side ``_sort_oldest_first`` behaviour.
    """
    if not states:
        return []
    placeholders = ",".join("?" * len(states))
    sql = f"""
        SELECT state_machine.item_id AS item_id,
               state_machine.state   AS state,
               state_machine.entered_at AS entered_at,
               items.title           AS title,
               items.created_at      AS created_at,
               items.author          AS author,
               items.summary         AS summary
          FROM state_machine
          JOIN items USING (item_id)
         WHERE state_machine.state IN ({placeholders})
           AND state_machine.item_id LIKE ?
         ORDER BY state_machine.entered_at ASC, state_machine.item_id ASC
         LIMIT ?
    """
    like = f"gh:{repo}#%"
    params = list(states) + [like, int(limit)]
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _row_to_issue_dict(r)
        if d:
            out.append(d)
    return out


def db_list_sm_issues(
    repo: str,
    *,
    db_path: Optional[pathlib.Path] = None,
) -> list[dict[str, Any]]:
    """DB equivalent of :func:`alice_forge.dispatcher.gh.gh_list_sm_issues`.

    Returns items at any non-terminal ``sm:*`` state. GH open/closed
    status is NOT tracked in the DB yet, so a closed-with-non-terminal
    label will appear here even though the GH query filters it out —
    the shadow diff will flag it; a follow-up PR adds the column.

    Raises :class:`sqlite3.Error` / :class:`OSError` on DB open failure
    so :func:`shadow_resolve` can distinguish "broken DB" (fail-open
    to GH) from "DB OK, no rows" (authoritative empty).
    """
    with _connect_state_db(db_path=db_path, read_only=True) as conn:
        return _list_items_in_states(
            conn,
            repo,
            NON_TERMINAL_SM_LABELS,
            limit=RECENT_ISSUE_LIMIT,
        )


def db_list_stale_closed_sm_issues(
    repo: str,
    *,
    db_path: Optional[pathlib.Path] = None,
) -> list[dict[str, Any]]:
    """DB equivalent of :func:`alice_forge.dispatcher.gh.gh_list_stale_closed_sm_issues`.

    The DB does not yet track GH open/closed status, so this helper
    cannot replicate the ``--state closed`` filter. It returns ``[]``
    for now — the shadow diff will log every GH-side entry as
    "missing from DB", which is the expected signal that a follow-up
    PR (track GH open/closed in a column on ``items`` or a sibling
    table) is needed before this sweep can cut over. Until then the
    sweep stays GH-authoritative even in ``ALICE_DISPATCHER_SHADOW=0``
    mode — see :func:`shadow_resolve` for the fallback wiring.

    Pure helper — does not touch the DB; never raises.
    """
    return []


def db_list_open_done_sm_issues(
    repo: str,
    *,
    db_path: Optional[pathlib.Path] = None,
) -> list[dict[str, Any]]:
    """DB equivalent of :func:`alice_forge.dispatcher.gh.gh_list_open_done_sm_issues`.

    Returns items at ``sm:done``. GH open/closed status not tracked —
    a *closed* sm:done (the normal terminal state) will also appear
    here, which the shadow diff will surface as noise. The caller
    (:func:`_process_open_done`) re-validates per-issue before
    acting, so false-positives are safe but burn one gh call each.
    Follow-up PR removes the false positives by tracking open status.

    Raises :class:`sqlite3.Error` / :class:`OSError` on DB open
    failure — same contract as :func:`db_list_sm_issues`.
    """
    with _connect_state_db(db_path=db_path, read_only=True) as conn:
        return _list_items_in_states(
            conn,
            repo,
            frozenset({DONE_SM_LABEL}),
            limit=RECENT_ISSUE_LIMIT,
        )


# ---------------------------------------------------------------------------
# Shadow comparison + divergence logging
# ---------------------------------------------------------------------------


def _default_shadow_notes_dir() -> pathlib.Path:
    """Resolve the ``inner/notes/`` directory from the state DB path.

    The state DB sits at ``~/alice-mind/inner/state/alice-state.db``;
    the notes inbox sits at ``~/alice-mind/inner/notes/``. Derive one
    from the other so a non-default alice-mind install (CI, tests)
    still writes shadow notes into the right tree.
    """
    return DEFAULT_DB_PATH.parent.parent / "notes"


def _issue_numbers(items: list[dict[str, Any]]) -> set[int]:
    """Project an issue list to the set of issue numbers.

    Both GH and DB results are dict-shaped with ``number`` keys;
    non-int numbers (malformed rows) are dropped silently rather
    than crashing the compare — the dispatcher's main loop already
    drops those.
    """
    out: set[int] = set()
    for i in items:
        n = i.get("number")
        if isinstance(n, int):
            out.add(n)
    return out


def _write_divergence_note(
    *,
    notes_dir: pathlib.Path,
    source_name: str,
    repo: str,
    gh_only: set[int],
    db_only: set[int],
    gh_count: int,
    db_count: int,
    log: Callable[[str], None],
) -> Optional[pathlib.Path]:
    """Persist one structured JSON note describing a divergence.

    One note per divergent call. The note carries enough shape to
    answer "which issue, which call, which side" without opening the
    DB. Tagged ``dispatcher-shadow`` so the memory worker drains it
    into the vault audit trail.
    """
    try:
        notes_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(
            f"[sm-dispatcher] shadow-diff: notes dir unavailable "
            f"({notes_dir}): {type(exc).__name__}: {exc}"
        )
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # Short random-ish suffix without pulling in ``secrets`` — the
    # dispatcher cadence is single-digit-seconds, two notes within
    # the same second is edge-case; a pid-based tie-breaker suffices.
    suffix = f"{os.getpid():x}"
    path = notes_dir / f"dispatcher-shadow-{ts}-{suffix}.json"
    payload = {
        "tag": "dispatcher-shadow",
        "source": source_name,
        "repo": repo,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gh_count": gh_count,
        "db_count": db_count,
        "gh_only_numbers": sorted(gh_only),
        "db_only_numbers": sorted(db_only),
        "divergent": True,
    }
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log(
            f"[sm-dispatcher] shadow-diff: failed to write note "
            f"{path}: {type(exc).__name__}: {exc}"
        )
        return None
    return path


def compare_shadow_results(
    *,
    gh_result: list[dict[str, Any]],
    db_result: list[dict[str, Any]],
    source_name: str,
    repo: str,
    notes_dir: Optional[pathlib.Path] = None,
    log: Callable[[str], None] = lambda s: None,
) -> bool:
    """Compare identity sets, log divergence, return True when divergent.

    ``source_name`` names the gh helper being shadowed
    (``gh_list_sm_issues``, ``gh_list_open_done_sm_issues``, …) so
    the stdout log line and the inner/notes/ filename both carry it.
    Only identity (issue numbers) is compared — labels / body / etc.
    are deliberately out of scope for the shadow because the DB
    fidelity gaps (see module docstring) would make a field-level
    compare perma-divergent.

    When ``notes_dir`` is omitted, defaults to
    ``~/alice-mind/inner/notes/`` derived from the state-DB path.
    Tests pass an explicit tmp dir.
    """
    gh_numbers = _issue_numbers(gh_result)
    db_numbers = _issue_numbers(db_result)
    gh_only = gh_numbers - db_numbers
    db_only = db_numbers - gh_numbers

    if not gh_only and not db_only:
        return False

    log(
        f"[sm-dispatcher] shadow-diff {source_name} repo={repo}: "
        f"gh={len(gh_numbers)} db={len(db_numbers)} "
        f"gh_only={sorted(gh_only)!r} db_only={sorted(db_only)!r}"
    )
    _write_divergence_note(
        notes_dir=notes_dir or _default_shadow_notes_dir(),
        source_name=source_name,
        repo=repo,
        gh_only=gh_only,
        db_only=db_only,
        gh_count=len(gh_numbers),
        db_count=len(db_numbers),
        log=log,
    )
    return True


# ---------------------------------------------------------------------------
# Shadow-mode + cutover resolver — the single hook the dispatcher calls
# ---------------------------------------------------------------------------


def shadow_resolve(
    *,
    gh_callable: Callable[[str], list[dict[str, Any]]],
    db_callable: Callable[[str], list[dict[str, Any]]],
    source_name: str,
    repo: str,
    notes_dir: Optional[pathlib.Path] = None,
    log: Callable[[str], None] = lambda s: None,
    shadow: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Run GH + DB per env mode, return the authoritative list.

    Behaviour by mode (via :func:`shadow_enabled` unless ``shadow`` is
    passed explicitly — tests inject the flag):

    * **Shadow (True):** GH is authoritative. DB is called for the
      diff; the DB error is swallowed and treated as "empty DB list"
      so a broken state DB doesn't take the dispatcher down.
      :func:`compare_shadow_results` writes the note + stdout line
      when the identity sets diverge.
    * **Cutover (False):** DB is authoritative. On DB failure, fall
      back to the GH callable (fail-open — keeps the dispatcher
      running). :func:`db_list_stale_closed_sm_issues` returns ``[]``
      by design; for that specific source the fall-back-to-GH path
      is the correct behaviour until the open/closed column lands.
    """
    use_shadow = shadow_enabled() if shadow is None else shadow

    if use_shadow:
        gh_result = gh_callable(repo)
        try:
            db_result = db_callable(repo)
        except Exception as exc:  # noqa: BLE001 — shadow must never raise
            log(
                f"[sm-dispatcher] shadow-diff: db callable raised for "
                f"{source_name}/{repo}: {type(exc).__name__}: {exc} "
                f"(treating as empty DB)"
            )
            db_result = []
        compare_shadow_results(
            gh_result=gh_result,
            db_result=db_result,
            source_name=source_name,
            repo=repo,
            notes_dir=notes_dir,
            log=log,
        )
        return gh_result

    # Cutover: DB authoritative with fail-open GH fallback.
    try:
        db_result = db_callable(repo)
    except Exception as exc:  # noqa: BLE001 — must fail open
        log(
            f"[sm-dispatcher] cutover: db callable raised for "
            f"{source_name}/{repo}: {type(exc).__name__}: {exc} "
            f"(fail-open → gh fallback)"
        )
        return gh_callable(repo)

    # The ``stale_closed`` sweep returns [] by design (see helper
    # docstring). In cutover mode that would lose the sweep entirely;
    # keep the GH call as the fallback until the open/closed column
    # lands. Detected by the source name so DB-ready sources are not
    # penalised.
    if source_name == "gh_list_stale_closed_sm_issues" and not db_result:
        return gh_callable(repo)

    return db_result


# Defensive re-export: tests import these to assert the identity
# of the public API surface.
__all__ = [
    "compare_shadow_results",
    "db_list_open_done_sm_issues",
    "db_list_sm_issues",
    "db_list_stale_closed_sm_issues",
    "shadow_enabled",
    "shadow_resolve",
]


# Silence unused-import warnings for members we re-export only for
# consumer typing convenience (SM_LABEL_WHITELIST / RECENT_ISSUE_LIMIT
# live in constants; readers can look them up there).
_ = SM_LABEL_WHITELIST
