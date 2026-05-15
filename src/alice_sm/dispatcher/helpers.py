"""Internal helpers used across the dispatcher state handlers.

These are the small, repeatedly-imported utilities that don't fit any
single handler. Pre-split, several of them were defined adjacent to the
first handler that used them; post-split they live here so each
handler module can import what it needs without pulling in unrelated
handler internals.

Notable consolidation: pre-split the dispatcher had two definitions of
:func:`_find_parsed_comment_of_type` (the second shadowed the first via
Python's last-binding-wins rule). At runtime only the second — the
more general ``expected_types: type | tuple[type, ...]`` form — was
ever resolved. The duplicate-but-narrower first def is dropped here;
behavior is unchanged because nothing held a direct reference to the
shadowed function.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable

from alice_sm.dispatcher.constants import STUDY_HINT_WRITTEN_PREFIX
from alice_sm.dispatcher.errors import GHCommandError
from alice_sm.dispatcher.trust import _label_names
from alice_sm.dispatcher.types import ListCommentsFn


# Issue #195: when an ``art:research_note`` worker finishes, it posts
# the same ``[SM] transition from=selected to=done reason=...`` audit
# comment that ``_process_selected`` would otherwise emit for a PR-bearing
# task. This is the canonical machine-readable "I finished writing the
# research note" signal. Recognizing it as a valid close-gate input is
# what makes issue #195's fix work — see :func:`_research_close_signal`.
_RESEARCH_WORKER_DONE_PREFIX = "[SM] transition from=selected to=done"


def _comment_author_login(comment: dict[str, Any]) -> str | None:
    """Pull the GitHub login off a ``gh issue view --json comments`` entry.

    ``gh`` returns the ``author`` field as ``{"login": "..."}``; older
    payloads or test fixtures sometimes use a bare string. Returns
    ``None`` on any shape we don't understand so the parser layer can
    apply its own trust check and reject.
    """
    author = comment.get("author") if isinstance(comment, dict) else None
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str):
            return login
    if isinstance(author, str):
        return author
    return None


def _has_prior_study_hint_audit(
    comments: list[dict[str, Any]],
    *,
    trusted_authors: frozenset[str],
) -> bool:
    """Return True iff any comment is a trusted-authored study-hint audit.

    Defense-in-depth dedup: if the local state file was reset, the
    audit comment is the only persistent record that the hint was
    already written. Trust is required so a random commenter pasting
    the prefix can't trick the dispatcher into skipping a real hint
    emission.
    """
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str) or not body.startswith(STUDY_HINT_WRITTEN_PREFIX):
            continue
        login = _comment_author_login(c)
        if isinstance(login, str) and login in trusted_authors:
            return True
    return False


def _matches_resolves_issue(value: Any, issue_number: int) -> bool:
    """Compare a frontmatter ``resolves_issue[s]`` value against an issue number.

    The vault's YAML frontmatter is hand-authored, so a single field can
    show up as an int (``resolves_issue: 212``), a bare string
    (``resolves_issue: 212``), or an octothorpe-prefixed string
    (``resolves_issue: "#212"``). All three should match.
    """
    if isinstance(value, bool):
        # Python booleans are ``int`` subclasses; ``True == 1`` would
        # otherwise spuriously match issue #1.
        return False
    if isinstance(value, int):
        return value == issue_number
    if isinstance(value, str):
        s = value.strip().lstrip("#").strip()
        if not s:
            return False
        try:
            return int(s) == issue_number
        except ValueError:
            return False
    return False


def _find_resolving_research_note(
    issue_number: int,
    research_dir: pathlib.Path,
) -> pathlib.Path | None:
    """Scan ``research_dir`` for a note whose frontmatter resolves ``issue_number``.

    Issue #212. Recognises two frontmatter shapes:

      * ``resolves_issue: <N>``  — scalar; matches when ``<N>`` parses
        to ``issue_number``.
      * ``resolves_issues: [<A>, <B>, ...]`` — flow list; matches when
        any element parses to ``issue_number``.

    Returns the lexicographically-first matching path so the result is
    stable across passes (the typical vault filename starts with
    ``YYYY-MM-DD``, so this is also chronologically-first in practice).
    Returns ``None`` if ``research_dir`` is missing or contains no
    matching note — the caller falls back to the existing comment-poll
    behaviour.

    Read failures on individual files are swallowed: a single
    unreadable note must not block the rest of the scan.
    """
    if not research_dir.is_dir():
        return None
    # Local import — :mod:`alice_indexer.yaml_lite` lives in a sibling
    # package and importing at module top would pull the indexer's
    # dependency surface into every dispatcher session.
    from alice_indexer.yaml_lite import split_frontmatter

    for path in sorted(research_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        if not fm:
            continue
        scalar = fm.get("resolves_issue")
        if scalar is not None and _matches_resolves_issue(scalar, issue_number):
            return path
        listed = fm.get("resolves_issues")
        if isinstance(listed, list):
            for v in listed:
                if _matches_resolves_issue(v, issue_number):
                    return path
    return None


def _current_art_label(
    issue: dict[str, Any], art_whitelist: frozenset[str]
) -> str | None:
    """Return the single whitelisted ``art:*`` label, or None if not exactly one.

    Used by :func:`_process_needs_study` to decide whether to swap the
    art label on study-complete. Multiple art labels or zero matches
    return None — the swap path treats either as "no current label to
    remove" and applies the parsed art label additively.
    """
    names = _label_names(issue)
    arts = [n for n in names if n.startswith("art:") and n in art_whitelist]
    if len(arts) != 1:
        return None
    return arts[0]


def _find_parsed_comment_of_type(
    comments: list[dict[str, Any]],
    expected_types: type | tuple[type, ...],
    *,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
):
    """Scan ``comments`` newest-first and return the first parsed match, or None.

    The trust check is enforced inside the parsers themselves
    (:mod:`alice_sm.comments`), so an untrusted commenter pasting one
    of the canonical verbs parses to ``None`` and is silently skipped.
    """
    from alice_sm.comments import parse_comment

    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        login = _comment_author_login(c)
        parsed = parse_comment(
            body,
            login,
            trusted_authors=trusted_authors,
            log=log,
        )
        if isinstance(parsed, expected_types):
            return parsed
    return None


def _research_close_signal(
    repo: str,
    number: int,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Inspect issue comments for a trusted close-gate signal (issues #174, #195).

    Returns ``(satisfied, reason_suffix)``:

      * ``satisfied`` — True iff a trusted comment on the issue justifies
        closing the OPEN + ``sm:done`` + ``art:research_note`` row.
      * ``reason_suffix`` — short human-readable tag describing *which*
        signal landed (``"exit-transition recorded"`` for the explicit
        ``[SM] exit-transition=<value>`` form, ``"worker self-transition
        to=done"`` for the worker's own ``[SM] transition`` audit
        comment). Used by :func:`_process_open_done` to render the close
        audit comment so the trail records why the gate opened.

    Two signals are accepted (in priority order):

    1. ``[SM] exit-transition=<value> findings=[[...]]?`` — the explicit
       "what should happen to this note next" verb introduced by #174
       (``disseminate`` | ``spawn-code`` | ``both``). Preferred when
       present because the value carries downstream-action metadata.

    2. ``[SM] transition from=selected to=done reason="..."`` — the
       worker's own audit comment, posted as instructed by the
       ``(sm:selected, art:research_note)`` dispatch row. This is the
       canonical "research-writer worker finished" signal and the only
       one any producer in this codebase actually emits today. Without
       this fallback the close path is dead-on-arrival (see #195).

    Pre-existing rows (closed manually after #181 shipped — #105, #178,
    #179, #180) have only signal 2 because no producer of signal 1 was
    ever wired up. The fallback closes them on the next dispatcher pass
    so the migration story is "manual close was a transitional hack" and
    not "manual close forever."

    Why not relax further (e.g. accept human prose like
    ``Exit-transition: disseminate``)? Because the rest of the SM
    protocol is strict machine-readable shapes and adding loose
    natural-language matching here invites false positives on adjacent
    issue threads. Signal 2 is the worker's contract-mandated audit
    comment from a trusted author — strict enough, broad enough.

    We import :mod:`alice_sm.comments` lazily to avoid the import cycle
    (comments imports ``TRUSTED_AUTHORS`` / ``ART_LABEL_WHITELIST`` from
    this module at top level).
    """
    from alice_sm import comments as cm  # local import — avoid cycle

    try:
        items = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to list comments for #{number} while "
            f"checking research-note close signal: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return False, None

    worker_done_seen = False
    for item in items:
        body = item.get("body")
        author = item.get("author")
        # ``gh issue view --json comments`` returns
        # ``[{"author": {"login": ...}, "body": ...}, ...]``. Accept the
        # bare-login shape for test-fixture readability.
        if isinstance(author, dict):
            login = author.get("login")
        elif isinstance(author, str):
            login = author
        else:
            login = None
        if not isinstance(body, str):
            continue
        # Signal 1 — explicit exit-transition verb (preferred). Take the
        # first hit and short-circuit; the explicit verb wins over the
        # implicit worker audit comment when both are present.
        parsed = cm.parse_exit_transition(
            body, login, trusted_authors=trusted_authors, log=lambda _m: None
        )
        if parsed is not None:
            return True, "exit-transition recorded"
        # Signal 2 — worker self-transition audit comment. Keep
        # scanning so signal 1 still wins if it shows up later in the
        # comment stream, but remember that we saw signal 2.
        if (
            body.startswith(_RESEARCH_WORKER_DONE_PREFIX)
            and login in trusted_authors
        ):
            worker_done_seen = True

    if worker_done_seen:
        return True, "worker self-transition to=done"
    return False, None


def _has_exit_transition_comment(
    repo: str,
    number: int,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
) -> bool:
    """Backwards-compatible boolean wrapper around :func:`_research_close_signal`.

    Pre-existing callers / tests that only need a yes-or-no answer
    continue to work. New code should use :func:`_research_close_signal`
    directly so the reason suffix is preserved in the audit trail.
    """
    satisfied, _reason = _research_close_signal(
        repo, number, list_comments, trusted_authors, log
    )
    return satisfied
