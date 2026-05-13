"""Parsers for the ``[SM]`` comment protocol used by the design + study workflows.

The dispatcher reads issue comments to discover state transitions emitted
by other agents (thinking-agent for design drafts and study notes, the
speaking daemon for design review verdicts, Sonnet for code review,
Jason for human overrides). Each comment is a single line that starts
with the ``[SM]`` sentinel followed by a verb and ``key=value`` pairs —
the same shape the dispatcher itself uses when posting
``[SM] dispatcher-hello`` / ``[SM] transition`` audit comments
(see :mod:`alice_sm.dispatcher`).

This module is the single source of truth for those shapes. Each parser:

  * matches its verb-specific prefix,
  * extracts required fields,
  * validates them (wikilink format, ``art:*`` whitelist membership,
    comment-author trust),
  * returns a frozen dataclass on success or ``None`` on any failure,
    logging a defensive warning describing the rejection.

Centralizing the parsers here avoids duplication across the design /
study handler sub-issues that wire the dispatcher state machine, and
gives us one place to evolve the protocol when a new verb is added.

Wikilink shape
--------------
Values written ``[[<target>]]`` are validated against
:data:`WIKILINK_RE`. We check format only — the vault note may still be
in flight when the comment is posted, so existence is not required.

Trust
-----
Each parser refuses to interpret a comment whose author is not in
``trusted_authors`` (default :data:`alice_sm.dispatcher.TRUSTED_AUTHORS`).
The comment-body ``author=`` tag (e.g. ``author=alice`` on
``design-ready``) is a separate field that records which subsystem
emitted the comment; it's parsed and returned, but it does NOT replace
the GitHub-author trust check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from alice_sm.dispatcher import ART_LABEL_WHITELIST, TRUSTED_AUTHORS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFIX = "[SM]"

# Allowed verdicts on ``[SM] code-review``. Mirrors the Sonnet code
# reviewer's JSON contract — see
# :class:`alice_speaking.review.code_reviewer.CodeReviewResult`.
CODE_REVIEW_VERDICTS: frozenset[str] = frozenset({"approved", "needs_revision"})

# Allowed values on ``[SM] exit-transition`` (issue #174). The
# research_note worker chooses one of these to declare what should
# happen with the note after it's written:
#
#   * ``disseminate`` — groom the note into cortex-memory (atomic notes,
#     wikilinks, decay-aware). The learning becomes durable memory.
#   * ``spawn-code`` — file a follow-up ``art:code`` issue scoped to the
#     implementation the findings imply.
#   * ``both``       — disseminate AND spawn code. Most likely for
#     substantive design work.
#
# The dispatcher's research_note close path requires one of these to be
# present from a trusted author before it will close the GH issue.
EXIT_TRANSITION_VALUES: frozenset[str] = frozenset(
    {"disseminate", "spawn-code", "both"}
)

# Wikilink target: anything non-empty that doesn't itself contain ``]]``.
# We deliberately do NOT enforce a slug/path shape — the vault accepts
# free-form note titles and the parser shouldn't second-guess what's
# valid there. Existence isn't checked either; the note may still be in
# flight when the comment is posted (the thinking-agent writes the
# comment and the note in the same pass; either may land first).
WIKILINK_RE = re.compile(r"^\[\[(?P<target>[^\]]+(?:\][^\]]+)*)\]\]$")

# Bare-word value (no whitespace). Used for ``art=art:code``,
# ``author=alice``, ``verdict=approved``.
_BAREWORD_RE = re.compile(r"^[A-Za-z0-9_:\-./]+$")

# Generic ``key="quoted value with spaces"`` or ``key=bareword``
# splitter. Order-tolerant: we re-key into a dict, so callers don't care
# whether ``reason`` or ``feedback`` came first on the line.
_KV_RE = re.compile(
    r"""
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)   # identifier
    =                                   # literal =
    (?:
        "(?P<qval>[^"]*)"               # double-quoted value
      | (?P<bval>\S+)                   # bareword value (incl. [[wikilink]])
    )
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignReady:
    """``[SM] design-ready note=[[...]] author=alice``."""

    note: str
    author: str


@dataclass(frozen=True)
class DesignApproved:
    """``[SM] design-approved``."""


@dataclass(frozen=True)
class DesignRevise:
    """``[SM] design-revise reason="..." feedback=[[...]]``."""

    reason: str
    feedback: str


@dataclass(frozen=True)
class DesignRejected:
    """``[SM] design-rejected reason="..."``."""

    reason: str


@dataclass(frozen=True)
class CodeReview:
    """``[SM] code-review verdict=approved findings=[[...]]``."""

    verdict: str
    findings: str


@dataclass(frozen=True)
class CodeReviewOverride:
    """``[SM] code-review-override reason="..."``."""

    reason: str


@dataclass(frozen=True)
class StudyComplete:
    """``[SM] study-complete art=art:code findings=[[...]]``.

    ``art`` may swap the issue's art label on exit from ``sm:needs_study``.
    """

    art_label: str
    findings: str


@dataclass(frozen=True)
class StudyBlocked:
    """``[SM] study-blocked reason="..."``."""

    reason: str


@dataclass(frozen=True)
class StudyProgress:
    """``[SM] study-progress note=[[...]]`` — checkpoint, resets the 7-day clock."""

    note: str


@dataclass(frozen=True)
class StudyRejected:
    """``[SM] study-rejected reason="..."``."""

    reason: str


@dataclass(frozen=True)
class RouteToStudy:
    """``[SM] route-to-study art=<art-label>?`` — sm:draft → sm:needs_study.

    The ``art`` field is optional. When present, the dispatcher swaps
    the issue's ``art:*`` label on the transition; the parsed value is
    already validated against
    :data:`alice_sm.dispatcher.ART_LABEL_WHITELIST`. When absent, the
    issue keeps its existing ``art:*`` label.
    """

    art_label: str | None = None


@dataclass(frozen=True)
class ReturnToStudy:
    """``[SM] return-to-study reason=<text>`` — sm:selected → sm:needs_study.

    Worker-emitted "I need thinking input before I can build" signal.
    ``reason`` is required so the audit trail records *why* the issue
    bounced back to the study lane.
    """

    reason: str


@dataclass(frozen=True)
class BuildStarted:
    """``[SM] build-started`` — thinking-agent emits after compaction, in BUILD mode.

    Signals to the dispatcher that the per-issue agent has finished
    compaction and is now implementing against the approved design. The
    dispatcher consumes it to transition ``sm:compacting`` →
    ``sm:building``.
    """


@dataclass(frozen=True)
class ExitTransition:
    """``[SM] exit-transition=<value> findings=[[...]]? spawned=<text>?``.

    Issue #174. Posted by the research-writer worker before requesting
    ``sm:done`` on an ``art:research_note`` issue. ``value`` is one of
    :data:`EXIT_TRANSITION_VALUES`; ``findings`` (if present) names the
    research-note vault wikilink; ``spawned`` (if present) records any
    follow-up issues the worker filed.

    The dispatcher's research_note close path requires this comment
    from a trusted author before it will close the GH issue — without
    it the lifecycle never completes and the work card stays in the
    viewer's open list.

    Two on-the-wire forms are accepted:

      * ``[SM] exit-transition=both ...``  (used in the wild on the
        retroactive #136/#148/#149/#170 closes)
      * ``[SM] exit-transition both ...``  (matches the canonical
        ``[SM] <verb> key=value`` shape used by every other parser)
    """

    value: str
    findings: str | None = None
    spawned: str | None = None


# Union over every parsed result type. Handlers that consume
# :func:`parse_comment` typically ``isinstance``-dispatch on this.
ParsedComment = (
    DesignReady
    | DesignApproved
    | DesignRevise
    | DesignRejected
    | CodeReview
    | CodeReviewOverride
    | StudyComplete
    | StudyBlocked
    | StudyProgress
    | StudyRejected
    | RouteToStudy
    | ReturnToStudy
    | BuildStarted
    | ExitTransition
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_log(msg: str) -> None:
    # Match the dispatcher's stderr-log convention so callers that don't
    # inject ``log`` still get rejections visible in the worker log.
    import sys

    print(msg, file=sys.stderr)


def _check_trust(
    comment_author: str | None,
    trusted_authors: frozenset[str],
    verb: str,
    log: Callable[[str], None],
) -> bool:
    if comment_author is None or comment_author not in trusted_authors:
        log(
            f"[sm-comments] rejecting {verb!r} from untrusted comment author "
            f"{comment_author!r}"
        )
        return False
    return True


def _extract_kv(body: str) -> dict[str, str]:
    """Pull every ``key=value`` token out of ``body`` into a dict.

    Duplicate keys take the *last* occurrence. Order on the wire is not
    significant — the dispatcher contract is ``key=value`` pairs, not a
    positional schema.
    """
    out: dict[str, str] = {}
    for match in _KV_RE.finditer(body):
        key = match.group("key")
        if match.group("qval") is not None:
            out[key] = match.group("qval")
        else:
            out[key] = match.group("bval")
    return out


def _strip_verb(body: str, verb: str) -> str | None:
    """Return the body after ``[SM] <verb>``, or None if the prefix doesn't match.

    Tolerates the trailing-whitespace case (``[SM] design-approved`` with
    nothing after) but requires either end-of-string or whitespace
    between the verb and the next field — ``design-approved-extra`` must
    NOT match ``design-approved``.
    """
    head = f"{PREFIX} {verb}"
    if not body.startswith(head):
        return None
    tail = body[len(head) :]
    if tail and not tail[0].isspace():
        return None
    return tail.strip()


def _parse_wikilink(value: str) -> str | None:
    m = WIKILINK_RE.match(value)
    if not m:
        return None
    return m.group("target")


# ---------------------------------------------------------------------------
# Per-verb parsers
# ---------------------------------------------------------------------------


def parse_design_ready(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> DesignReady | None:
    """``[SM] design-ready note=[[<wikilink>]] author=alice``.

    Emitted by the thinking-agent when a design draft is ready for the
    speaking review gate.
    """
    tail = _strip_verb(body, "design-ready")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "design-ready", log):
        return None
    kv = _extract_kv(tail)
    note_raw = kv.get("note")
    author = kv.get("author")
    if not note_raw or not author:
        log(
            f"[sm-comments] design-ready missing required fields: "
            f"note={note_raw!r} author={author!r}"
        )
        return None
    target = _parse_wikilink(note_raw)
    if target is None:
        log(f"[sm-comments] design-ready note is not a wikilink: {note_raw!r}")
        return None
    if not _BAREWORD_RE.match(author):
        log(f"[sm-comments] design-ready author is not a bareword: {author!r}")
        return None
    return DesignReady(note=target, author=author)


def parse_design_approved(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> DesignApproved | None:
    """``[SM] design-approved`` — speaking-emitted, advances to ``sm:designed``."""
    tail = _strip_verb(body, "design-approved")
    if tail is None:
        return None
    if tail:
        log(f"[sm-comments] design-approved has unexpected trailing content: {tail!r}")
        return None
    if not _check_trust(comment_author, trusted_authors, "design-approved", log):
        return None
    return DesignApproved()


def parse_design_revise(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> DesignRevise | None:
    """``[SM] design-revise reason="..." feedback=[[<wikilink>]]``."""
    tail = _strip_verb(body, "design-revise")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "design-revise", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    feedback_raw = kv.get("feedback")
    if not reason or not feedback_raw:
        log(
            f"[sm-comments] design-revise missing required fields: "
            f"reason={reason!r} feedback={feedback_raw!r}"
        )
        return None
    target = _parse_wikilink(feedback_raw)
    if target is None:
        log(f"[sm-comments] design-revise feedback is not a wikilink: {feedback_raw!r}")
        return None
    return DesignRevise(reason=reason, feedback=target)


def parse_design_rejected(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> DesignRejected | None:
    """``[SM] design-rejected reason="..."`` — transitions to ``sm:rejected``."""
    tail = _strip_verb(body, "design-rejected")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "design-rejected", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    if not reason:
        log("[sm-comments] design-rejected missing reason field")
        return None
    return DesignRejected(reason=reason)


def parse_code_review(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> CodeReview | None:
    """``[SM] code-review verdict=approved findings=[[<wikilink>]]``.

    Sonnet-emitted on the PR. ``verdict`` must be in
    :data:`CODE_REVIEW_VERDICTS`.

    The prefix-match guards against confusion with
    ``[SM] code-review-override``: ``_strip_verb`` requires whitespace or
    end-of-string after the verb, so ``code-review-override`` does not
    match the ``code-review`` head.
    """
    tail = _strip_verb(body, "code-review")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "code-review", log):
        return None
    kv = _extract_kv(tail)
    verdict = kv.get("verdict")
    findings_raw = kv.get("findings")
    if not verdict or not findings_raw:
        log(
            f"[sm-comments] code-review missing required fields: "
            f"verdict={verdict!r} findings={findings_raw!r}"
        )
        return None
    if verdict not in CODE_REVIEW_VERDICTS:
        log(
            f"[sm-comments] code-review unknown verdict {verdict!r} — "
            f"expected one of {sorted(CODE_REVIEW_VERDICTS)}"
        )
        return None
    target = _parse_wikilink(findings_raw)
    if target is None:
        log(f"[sm-comments] code-review findings is not a wikilink: {findings_raw!r}")
        return None
    return CodeReview(verdict=verdict, findings=target)


def parse_code_review_override(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> CodeReviewOverride | None:
    """``[SM] code-review-override reason="..."`` — bypasses the sonnet gate."""
    tail = _strip_verb(body, "code-review-override")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "code-review-override", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    if not reason:
        log("[sm-comments] code-review-override missing reason field")
        return None
    return CodeReviewOverride(reason=reason)


def parse_study_complete(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    art_whitelist: frozenset[str] = ART_LABEL_WHITELIST,
    log: Callable[[str], None] = _default_log,
) -> StudyComplete | None:
    """``[SM] study-complete art=<art-label> findings=[[<wikilink>]]``.

    Exits ``sm:needs_study`` to ``sm:selected``. The ``art`` value must
    be in :data:`alice_sm.dispatcher.ART_LABEL_WHITELIST`; the dispatcher
    may use it to swap the issue's art label as part of the transition.
    """
    tail = _strip_verb(body, "study-complete")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "study-complete", log):
        return None
    kv = _extract_kv(tail)
    art_label = kv.get("art")
    findings_raw = kv.get("findings")
    if not art_label or not findings_raw:
        log(
            f"[sm-comments] study-complete missing required fields: "
            f"art={art_label!r} findings={findings_raw!r}"
        )
        return None
    if art_label not in art_whitelist:
        log(
            f"[sm-comments] study-complete art label {art_label!r} not in "
            f"whitelist {sorted(art_whitelist)}"
        )
        return None
    target = _parse_wikilink(findings_raw)
    if target is None:
        log(
            f"[sm-comments] study-complete findings is not a wikilink: {findings_raw!r}"
        )
        return None
    return StudyComplete(art_label=art_label, findings=target)


def parse_study_blocked(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> StudyBlocked | None:
    """``[SM] study-blocked reason="..."`` — transitions to ``sm:blocked``."""
    tail = _strip_verb(body, "study-blocked")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "study-blocked", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    if not reason:
        log("[sm-comments] study-blocked missing reason field")
        return None
    return StudyBlocked(reason=reason)


def parse_study_progress(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> StudyProgress | None:
    """``[SM] study-progress note=[[<wikilink>]]`` — checkpoint comment."""
    tail = _strip_verb(body, "study-progress")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "study-progress", log):
        return None
    kv = _extract_kv(tail)
    note_raw = kv.get("note")
    if not note_raw:
        log("[sm-comments] study-progress missing note field")
        return None
    target = _parse_wikilink(note_raw)
    if target is None:
        log(f"[sm-comments] study-progress note is not a wikilink: {note_raw!r}")
        return None
    return StudyProgress(note=target)


def parse_study_rejected(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> StudyRejected | None:
    """``[SM] study-rejected reason="..."`` — transitions to ``sm:rejected``."""
    tail = _strip_verb(body, "study-rejected")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "study-rejected", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    if not reason:
        log("[sm-comments] study-rejected missing reason field")
        return None
    return StudyRejected(reason=reason)


def parse_route_to_study(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    art_whitelist: frozenset[str] = ART_LABEL_WHITELIST,
    log: Callable[[str], None] = _default_log,
) -> RouteToStudy | None:
    """``[SM] route-to-study art=<art-label>?`` — sm:draft → sm:needs_study.

    The bare form (no fields) is the common case: the issue keeps its
    current ``art:*`` label across the transition. ``art=<label>`` is
    optional and must be whitelisted when present; the dispatcher uses
    it to swap the issue's art label as part of the transition.
    """
    tail = _strip_verb(body, "route-to-study")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "route-to-study", log):
        return None
    if not tail:
        return RouteToStudy(art_label=None)
    kv = _extract_kv(tail)
    unexpected = set(kv) - {"art"}
    if unexpected or "art" not in kv:
        log(
            f"[sm-comments] route-to-study has unexpected trailing content: {tail!r}"
        )
        return None
    art_label = kv["art"]
    if art_label not in art_whitelist:
        log(
            f"[sm-comments] route-to-study art label {art_label!r} not in "
            f"whitelist {sorted(art_whitelist)}"
        )
        return None
    return RouteToStudy(art_label=art_label)


def parse_return_to_study(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> ReturnToStudy | None:
    """``[SM] return-to-study reason=<text>`` — sm:selected → sm:needs_study.

    ``reason`` is required: the worker-emitted "I need thinking input"
    signal must record why the issue couldn't be advanced from
    ``sm:selected``, so the audit trail explains the reversal.
    """
    tail = _strip_verb(body, "return-to-study")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "return-to-study", log):
        return None
    kv = _extract_kv(tail)
    reason = kv.get("reason")
    if not reason:
        log("[sm-comments] return-to-study missing reason field")
        return None
    return ReturnToStudy(reason=reason)


def parse_build_started(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> BuildStarted | None:
    """``[SM] build-started`` — thinking-agent emits after compaction.

    Bare verb (no fields). The dispatcher consumes the prefix as the
    sm:compacting → sm:building signal; a ``task=#N`` or ``ts=...``
    suffix is tolerated (the agent may render an audit-style payload)
    but not required.
    """
    tail = _strip_verb(body, "build-started")
    if tail is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "build-started", log):
        return None
    return BuildStarted()


# ``[SM] exit-transition`` allows ``=`` between verb and value as well as
# whitespace — the wild-form (#136/#148/#149/#170 retroactive closes)
# used ``exit-transition=both``, and the canonical bareword shape is
# ``exit-transition both``. Both must parse to the same dataclass.
_EXIT_TRANSITION_HEAD_RE = re.compile(
    r"^\[SM\]\s+exit-transition(?P<sep>[=\s])(?P<rest>.*)$",
    re.DOTALL,
)


def parse_exit_transition(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    valid_values: frozenset[str] = EXIT_TRANSITION_VALUES,
    log: Callable[[str], None] = _default_log,
) -> ExitTransition | None:
    """``[SM] exit-transition=<value> findings=[[...]]? spawned=<text>?``.

    Issue #174 — research_note close-path enforcement. ``value`` must be
    one of :data:`EXIT_TRANSITION_VALUES`. ``findings`` (when present)
    must be a wikilink. ``spawned`` is opaque free-form text the parser
    captures but doesn't validate (it can contain ``#N`` lists or prose).

    Accepted shapes:

      * ``[SM] exit-transition=both``
      * ``[SM] exit-transition both findings=[[note]]``
      * ``[SM] exit-transition=both findings=[[note]] spawned="#150 #151"``
      * ``[SM] exit-transition=both findings=[[note]] spawned=#150 #151``

    The trailing ``spawned=`` value, when bareword, swallows the rest of
    the line so a worker can drop a free-form ``#150 #151 #152`` list
    without quoting. ``findings`` and ``spawned`` are both optional —
    only ``value`` is required.
    """
    m = _EXIT_TRANSITION_HEAD_RE.match(body)
    if m is None:
        return None
    if not _check_trust(comment_author, trusted_authors, "exit-transition", log):
        return None

    rest = m.group("rest").strip()
    if not rest:
        log("[sm-comments] exit-transition missing value")
        return None

    # First token after the verb is the value. After ``exit-transition=``
    # the value is the first whitespace-delimited token; after
    # ``exit-transition <value>`` it's the same shape. Tokens that look
    # like ``key=val`` belong to the kv tail, so the head must be a
    # bareword without ``=`` inside.
    head, _, tail = rest.partition(" ")
    if "=" in head:
        # Caller wrote ``exit-transition value=both`` — accept the kv form.
        kv = _extract_kv(rest)
        value = kv.get("value")
        if value is None:
            log(
                f"[sm-comments] exit-transition kv-form missing 'value': {rest!r}"
            )
            return None
        findings_raw = kv.get("findings")
        spawned = kv.get("spawned")
    else:
        value = head
        findings_raw = None
        spawned = None
        if tail.strip():
            kv = _extract_kv(tail)
            findings_raw = kv.get("findings")
            spawned = kv.get("spawned")
            # ``spawned=#150 #151`` (bareword + trailing tokens) is
            # common in the wild. ``_extract_kv`` captures only the
            # first bareword token. Re-scan the original tail to recover
            # everything after ``spawned=`` so the audit trail keeps the
            # full list intact.
            spawn_idx = tail.find("spawned=")
            if spawn_idx >= 0:
                trailing = tail[spawn_idx + len("spawned=") :].strip()
                # Stop at the next ``key=`` so we don't swallow another
                # field. ``findings=`` is the only other field today; if
                # we add more, this needs widening.
                cut = trailing.find(" findings=")
                if cut >= 0:
                    trailing = trailing[:cut].strip()
                if trailing:
                    spawned = trailing

    if value not in valid_values:
        log(
            f"[sm-comments] exit-transition unknown value {value!r} — "
            f"expected one of {sorted(valid_values)}"
        )
        return None

    findings: str | None = None
    if findings_raw is not None:
        findings = _parse_wikilink(findings_raw)
        if findings is None:
            log(
                f"[sm-comments] exit-transition findings is not a wikilink: "
                f"{findings_raw!r}"
            )
            return None

    return ExitTransition(value=value, findings=findings, spawned=spawned)


# ---------------------------------------------------------------------------
# Issue #176 — dependency parser
# ---------------------------------------------------------------------------
#
# The dispatcher gates spawns on resolution of cross-issue dependencies
# declared in plain prose on the issue body (and amendment comments).
# Format: a line that begins with one of the recognized verbs followed by
# one or more ``#N`` references (comma-separated is fine).
#
#     Depends on #5
#     Blocked by #5, #6
#     Soft depends on #5
#
# Hard verbs gate the spawn; soft verbs are captured but informational
# only. Case-insensitive. Anchored to the start of the (lstripped) line
# so prose like "If this requires #5 ..." does not produce a false
# positive — the convention is that humans put the directive on its own
# line.

# Hard verbs gate spawning. "soft depends on" must come before "depends on"
# in the alternation so the regex engine picks the longer match first.
_HARD_DEP_VERBS: tuple[str, ...] = (
    "depends on",
    "blocked by",
    "requires",
    "waits for",
)
_SOFT_DEP_VERBS: tuple[str, ...] = (
    "soft depends on",
    "prefers",
)

_DEP_VERB_RE = re.compile(
    r"^(?P<verb>"
    + "|".join(re.escape(v) for v in (*_SOFT_DEP_VERBS, *_HARD_DEP_VERBS))
    + r")\b",
    re.IGNORECASE,
)

# Issue references on the rest of the line: ``#N`` tokens, comma-separated
# or whitespace-separated. We match each ``#N`` independently so
# ``Depends on #5, #6 and #7`` picks up all three.
_ISSUE_REF_RE = re.compile(r"#(?P<num>\d+)")


@dataclass(frozen=True)
class IssueDependencies:
    """Hard and soft dependency issue numbers parsed from prose.

    ``hard`` gates the dispatcher spawn (every ``hard`` ref must be
    closed at ``sm:done`` before the issue is eligible). ``soft`` is
    informational only — useful for "I'd prefer #N to land first but
    it's not strictly required" — and does not block spawning.

    Both lists are de-duplicated and preserve first-seen order so the
    log line and any future viewer rendering are deterministic.
    """

    hard: tuple[int, ...]
    soft: tuple[int, ...]


def parse_dependencies(text: str | None) -> IssueDependencies:
    """Scan prose for ``Depends on #N`` / ``Blocked by #N`` / etc.

    Recognized hard verbs: ``Depends on``, ``Blocked by``, ``Requires``,
    ``Waits for``. Soft verbs: ``Soft depends on``, ``Prefers``. All
    case-insensitive. The verb must start the (lstripped) line — prose
    like "but this requires #N to land first" is intentionally not
    picked up.

    Comma- or whitespace-separated continuations on the same line are
    captured, so::

        Depends on #5, #6 and #7

    yields ``hard == (5, 6, 7)``.
    """
    if not text:
        return IssueDependencies(hard=(), soft=())
    hard: list[int] = []
    soft: list[int] = []
    seen_hard: set[int] = set()
    seen_soft: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        m = _DEP_VERB_RE.match(line)
        if m is None:
            continue
        verb = m.group("verb").lower()
        is_soft = verb in _SOFT_DEP_VERBS
        rest = line[m.end() :]
        for ref in _ISSUE_REF_RE.finditer(rest):
            n = int(ref.group("num"))
            if is_soft:
                if n in seen_hard or n in seen_soft:
                    # If the same #N appears as both hard and soft,
                    # hard wins; if it's already in soft we don't
                    # double-count.
                    continue
                seen_soft.add(n)
                soft.append(n)
            else:
                if n in seen_hard:
                    continue
                # Promote: a previously-soft ref now declared hard
                # moves out of the soft list.
                if n in seen_soft:
                    seen_soft.discard(n)
                    soft.remove(n)
                seen_hard.add(n)
                hard.append(n)
    return IssueDependencies(hard=tuple(hard), soft=tuple(soft))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


# Order matters: longer-verb prefixes must be tried before their shorter
# prefixes (``code-review-override`` before ``code-review``). The
# per-parser :func:`_strip_verb` already enforces a whitespace/EOL
# boundary after the verb, but listing the longer verb first short-
# circuits the dispatch and keeps the log noise to one line.
_PARSERS: tuple[tuple[str, Callable[..., ParsedComment | None]], ...] = (
    ("design-ready", parse_design_ready),
    ("design-approved", parse_design_approved),
    ("design-revise", parse_design_revise),
    ("design-rejected", parse_design_rejected),
    ("code-review-override", parse_code_review_override),
    ("code-review", parse_code_review),
    ("study-complete", parse_study_complete),
    ("study-blocked", parse_study_blocked),
    ("study-progress", parse_study_progress),
    ("study-rejected", parse_study_rejected),
    ("route-to-study", parse_route_to_study),
    ("return-to-study", parse_return_to_study),
    ("build-started", parse_build_started),
    ("exit-transition", parse_exit_transition),
)


def parse_comment(
    body: str,
    comment_author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    log: Callable[[str], None] = _default_log,
) -> ParsedComment | None:
    """Try every parser, return the first match, or ``None``.

    Comments that don't start with the ``[SM]`` sentinel return ``None``
    silently — most issue comments are ordinary human prose and aren't
    meant for the dispatcher to consume. We only log when the sentinel
    matched but the body didn't validate, since that signals a malformed
    agent emission worth surfacing.
    """
    if not body.startswith(PREFIX):
        return None
    for verb, parser in _PARSERS:
        head = f"{PREFIX} {verb}"
        if not body.startswith(head):
            continue
        # The verb must terminate at a word boundary — ``exit-transition``
        # admits ``=`` here (wild-form on #174) in addition to whitespace
        # / EOL; every other verb only sees whitespace or EOL because the
        # per-parser :func:`_strip_verb` rejects the rest.
        next_ch = body[len(head) : len(head) + 1]
        if next_ch and next_ch not in (" ", "\t", "\n", "="):
            continue
        # Each parser re-validates its own prefix — the startswith above
        # is a cheap pre-filter that lets us pick the right parser
        # without trial-and-error across all 12 verbs.
        return parser(
            body,
            comment_author,
            trusted_authors=trusted_authors,
            log=log,
        )
    return None
