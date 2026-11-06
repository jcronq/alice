"""Per-task effort-level dial for SM-dispatched workers (issue #642).

Each SM-tracked issue carries an *effort level* that tunes how deep the
dispatched worker goes. The level is read from the issue by
:func:`read_effort` and threaded through
:func:`alice_forge.dispatcher.spawn.compose_spawn_prompt` as an extra
"Effort" rules block.

Resolution order (highest precedence first):

1. The most-recent ``[SM] effort=<level>`` verb comment from a trusted
   author. Mid-flight amendments beat the label.
2. The highest-precedence ``effort:<level>`` label on the issue. If
   multiple ``effort:*`` labels are present (shouldn't happen in
   practice; GitHub doesn't ban it), the strongest level wins — max
   over high over medium over low — so an operator can't silently
   drop effort by stacking labels.
3. :data:`DEFAULT` (``"medium"``). Matches current pre-#642 behavior.

The verb-over-label precedence intentionally mirrors
:func:`alice_forge.dispatcher.helpers._apply_latest_amendments` so
Jason can post ``[SM] effort=max`` on an existing task and see the
next spawn pick it up without a label edit.

Slice-1 scope: label read + verb read + prompt-block injection. The
status-site pill-to-cycle UI and worker self-escalation ride on later
slices (see #642 for the full list).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

Effort = Literal["low", "medium", "high", "max"]

#: Default effort level when no label and no verb are set. Matches
#: the current SM-dispatcher behavior pre-#642.
DEFAULT: Effort = "medium"

#: Every valid :data:`Effort` value in weakest→strongest order. Used
#: by :func:`_strongest` to pick the winning label when an issue
#: accidentally carries multiple ``effort:*`` labels.
LEVELS: tuple[Effort, ...] = ("low", "medium", "high", "max")

#: ``effort:<level>`` label prefix on the GH issue. :func:`read_effort`
#: picks the highest-precedence match from the label set.
LABEL_PREFIX = "effort:"

#: Regex matching an ``[SM] effort=<level>`` verb comment. Anchored at
#: the start of the body so a casual ``[SM] effort=high`` inline in a
#: longer prose comment doesn't accidentally flip the dial. Case-
#: sensitive on the level token to mirror the parser's strictness.
_VERB_RE = re.compile(
    r"^\[SM\]\s+effort=(?P<level>low|medium|high|max)\b"
)


# ---------------------------------------------------------------------------
# Low-level readers — kept separately testable
# ---------------------------------------------------------------------------


def _strongest(levels: list[Effort]) -> Effort | None:
    """Return the strongest level in ``levels`` by :data:`LEVELS` order.

    ``None`` when ``levels`` is empty — the caller decides the fallback.
    """
    if not levels:
        return None
    rank = {lvl: i for i, lvl in enumerate(LEVELS)}
    return max(levels, key=lambda lvl: rank.get(lvl, -1))


def effort_from_labels(labels: list[dict[str, Any]]) -> Effort | None:
    """Pick the highest-precedence ``effort:*`` label from ``labels``.

    ``labels`` is the raw ``gh issue view --json labels`` shape: a list
    of ``{"name": "<label>"}`` dicts. Entries without a string ``name``
    are skipped silently (defensive — the gh JSON occasionally carries
    empty objects on edge cases). Returns ``None`` if no ``effort:*``
    label is set.
    """
    hits: list[Effort] = []
    for lbl in labels or []:
        if not isinstance(lbl, dict):
            continue
        name = lbl.get("name")
        if not isinstance(name, str) or not name.startswith(LABEL_PREFIX):
            continue
        suffix = name[len(LABEL_PREFIX):]
        if suffix in LEVELS:
            hits.append(suffix)  # type: ignore[arg-type]
    return _strongest(hits)


def effort_from_comments(
    comments: list[dict[str, Any]],
    *,
    trusted_authors: frozenset[str] | None = None,
) -> Effort | None:
    """Scan ``comments`` for the most-recent ``[SM] effort=<level>`` verb.

    ``comments`` is the GH comment list shape used throughout the
    dispatcher — ``{"body": str, "author": {"login": str}}`` entries in
    chronological (oldest-first) order. The newest matching comment
    wins so a late amendment overrides an earlier one.

    When ``trusted_authors`` is set, comments from untrusted logins are
    ignored. ``None`` bypasses the check — handy for tests. In the live
    dispatcher, the caller passes the dispatcher's
    :data:`TRUSTED_AUTHORS` frozenset.
    """
    newest: Effort | None = None
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        if trusted_authors is not None:
            author = _author_login(c)
            if author is None or author not in trusted_authors:
                continue
        m = _VERB_RE.match(body.lstrip())
        if m is None:
            continue
        newest = m.group("level")  # type: ignore[assignment]
    return newest


def _author_login(comment: dict[str, Any]) -> str | None:
    """Extract ``author.login`` from a GH comment payload, or ``None``.

    Mirrors :func:`alice_forge.dispatcher.trust._author_login` but
    scoped to comment-shape payloads (as opposed to issue payloads).
    """
    author = comment.get("author")
    if not isinstance(author, dict):
        return None
    login = author.get("login")
    if isinstance(login, str) and login:
        return login
    return None


# ---------------------------------------------------------------------------
# Public reader — the one the dispatcher calls
# ---------------------------------------------------------------------------


def read_effort(
    issue_number: int,
    repo: str,
    *,
    get_issue: Callable[[str, int], dict[str, Any] | None] | None = None,
    list_comments: Callable[[str, int], list[dict[str, Any]]] | None = None,
    trusted_authors: frozenset[str] | None = None,
) -> Effort:
    """Determine the effort level for ``repo``'s issue ``issue_number``.

    Order of precedence (strongest first):

      1. Most-recent ``[SM] effort=<level>`` verb comment from a trusted
         author.
      2. Highest-rank ``effort:<level>`` label on the issue.
      3. :data:`DEFAULT`.

    ``get_issue`` / ``list_comments`` are injected so tests don't need
    to monkeypatch module-level globals. The live dispatcher passes
    :func:`alice_forge.dispatcher.gh.gh_get_issue` and
    :func:`alice_forge.dispatcher.gh.gh_list_issue_comments`.

    Failures in either call (gh non-zero exit, 404, transport error)
    degrade gracefully — the function returns :data:`DEFAULT` rather
    than raising, so a transient network blip can't block spawns.
    """
    if get_issue is None or list_comments is None:
        # Route the default fetchers through the dispatcher module
        # namespace so existing test monkeypatches (``sm.gh_get_issue``,
        # ``sm.gh_list_issue_comments``, and the ``sm._run_gh`` shim
        # that the raw ``gh_*`` helpers call) continue to short-circuit
        # the gh subprocess layer. A direct import from
        # ``alice_forge.dispatcher.gh`` would bind the unpatched names
        # at this call site and defeat the monkeypatch.
        import sys as _sys

        _dispatch_mod = _sys.modules.get("alice_forge.dispatcher")
        if get_issue is None:
            if _dispatch_mod is not None and hasattr(
                _dispatch_mod, "gh_get_issue"
            ):
                get_issue = _dispatch_mod.gh_get_issue
            else:
                from alice_forge.dispatcher.gh import gh_get_issue

                get_issue = gh_get_issue
        if list_comments is None:
            if _dispatch_mod is not None and hasattr(
                _dispatch_mod, "gh_list_issue_comments"
            ):
                list_comments = _dispatch_mod.gh_list_issue_comments
            else:
                from alice_forge.dispatcher.gh import (
                    gh_list_issue_comments,
                )

                list_comments = gh_list_issue_comments

    if trusted_authors is None:
        from alice_forge.dispatcher.constants import TRUSTED_AUTHORS

        trusted_authors = TRUSTED_AUTHORS

    # Verb beats label — evaluate it first so a mid-flight amendment
    # wins even when a label is also set.
    try:
        comments = list_comments(repo, issue_number)
    except Exception:
        comments = []
    verb_level = effort_from_comments(
        comments, trusted_authors=trusted_authors
    )
    if verb_level is not None:
        return verb_level

    try:
        issue = get_issue(repo, issue_number)
    except Exception:
        issue = None
    if isinstance(issue, dict):
        labels = issue.get("labels") or []
        if isinstance(labels, list):
            label_level = effort_from_labels(labels)
            if label_level is not None:
                return label_level

    return DEFAULT


# ---------------------------------------------------------------------------
# Prompt-block injection
# ---------------------------------------------------------------------------


_LOW_RULES = (
    "## Effort: low (ship-and-iterate)\n"
    "Smallest viable slice. Open a draft PR and mark it ready "
    "immediately if the fix is obvious. Fix-forward on the next "
    "slice rather than over-testing this one. Don't bulletproof."
)

_HIGH_RULES = (
    "## Effort: high (thorough)\n"
    "Add tests covering edge cases NOT in the spec. Run the full "
    "test suite. Run the regression suite via the ``code-review`` "
    "skill. Only merge when every check is green."
)

_MAX_RULES = (
    "## Effort: max (go-deep-until-done)\n"
    "Iterate until every open question is resolved. Spawn "
    "sub-reviewers if a fresh pair of eyes helps. Run "
    "``code-review max``. Close every TODO/FIXME you touch. Keep "
    "working through failure modes until the change is stable; "
    "stay silent until the task is complete."
)

#: Per-level rules block injected into the worker prompt. Medium renders
#: as the empty string so the baseline prompt stays byte-identical to
#: pre-#642 — the existing golden fixtures continue to match without
#: regeneration.
EFFORT_RULES: dict[Effort, str] = {
    "low": _LOW_RULES,
    "medium": "",
    "high": _HIGH_RULES,
    "max": _MAX_RULES,
}


def effort_rules_block(effort: Effort) -> str:
    """Return the "## Effort" rules block for ``effort``.

    Medium → empty string (no block). Low/high/max → the pinned rules
    strings above. Unknown values collapse to the medium (empty) block
    so a future enum drift can't produce a prompt with stray
    placeholders — the caller is responsible for validating the level
    against :data:`LEVELS`.
    """
    return EFFORT_RULES.get(effort, "")
