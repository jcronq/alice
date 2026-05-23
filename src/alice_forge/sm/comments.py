"""Loud-failure comment parser for SM v3.

Invariant I-6: any comment whose body starts with ``[SM] `` either
parses to a known verb (returning :class:`ParsedVerb`) or returns a
:class:`ParseError` that the dispatcher posts as a reply comment on
the issue. No silent stderr logging, no swallowed failures.

The parser is intentionally narrow: it knows the verb grammar and
the trust check, nothing else. The dispatcher applies parsed verbs
against the :data:`TRANSITIONS` table; verbs that don't appear in
the source state's transitions surface as a different kind of error
(``UnknownTransition``).

Comments that don't start with ``[SM] `` return ``None`` — ordinary
human prose is not the parser's concern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from alice_forge.sm.transitions import Verbs

# Reuse v1's trust set so the cutover doesn't change who can drive
# transitions. The dispatcher imports TRUSTED_AUTHORS from the v1
# constants module until Phase 4 retires v1; then we can promote
# the set into a v3-native constant.
TRUSTED_AUTHORS_DEFAULT: frozenset[str] = frozenset({"jcronq", "alice"})

PREFIX = "[SM]"

# Dispatcher self-audit prefixes — comments emitted by the dispatcher
# itself as protocol-internal announcements, not transition verbs.
# Mirrors the prefixes defined in ``alice_forge.dispatcher.constants``
# but kept here as a v3-side copy to avoid a cyclic import. Any new
# audit prefix that v1's spawn / hello / rebase machinery emits needs
# to be added in both places.
#
# Comments whose body starts with one of these prefixes return
# ``None`` from :func:`parse_comment` (treated like ordinary prose)
# rather than ``ParseError`` (unknown verb). Without this, every
# poll cycle on an issue with recent audit comments would post a
# fresh ``[SM] parse-error`` reply — the same noise that masked the
# orphan-design-ready spawn loop on #295.
AUDIT_PREFIXES: tuple[str, ...] = (
    "[SM] spawn-started",
    "[SM] thinking-spawn-started",
    "[SM] speaking-spawn-started",
    "[SM] dispatcher-hello",
    "[SM] study-hint-written",
    "[SM] design-ready-audit",
    "[SM] transition",
    "[SM] parse-error",
    "[SM] rebase-needed",
    "[SM] rebase-pushed",
    "[SM] rebase-escalated",
    "[SM] verify",
    "[SM] exit-transition-required",
    "[SM] design-revisions-capped",
    "[SM] auto-study-complete",
    # Legacy v0/v1 operator pattern — manual ``[SM] blocked
    # reason="..."`` shortcut used before ``[SM] transition
    # from=X to=blocked`` became the canonical audit. Still present
    # on old issues (e.g. #197); not a valid verb in v3's grammar.
    # Filtered here so the v3 parser doesn't emit a parse-error reply
    # every poll cycle the comment is still visible.
    "[SM] blocked",
    # Legacy speaking-build completion echo. Predates the v3
    # event-driven build-complete; some older comments still carry it.
    "[SM] speaking-build-complete",
)


def is_audit_prefix(body: str) -> bool:
    """True if ``body`` is a dispatcher-self audit comment, not a verb.

    Audit comments carry the ``[SM] `` prefix but are protocol-internal
    announcements (spawn started, label transitioned, etc.) — they
    must not be parsed as transition verbs or every poll would post a
    parse-error reply.
    """
    return any(body.startswith(p) for p in AUDIT_PREFIXES)


# art:* label whitelist — mirrors v1's ART_LABEL_WHITELIST. Kept
# here as a module-level default; tests can override.
ART_LABEL_WHITELIST_DEFAULT: frozenset[str] = frozenset(
    {"art:code", "art:research_note", "art:experiment", "art:config_change"}
)


# ----------------------------------------------------------------
# Parsed result types
# ----------------------------------------------------------------


@dataclass(frozen=True)
class ParsedVerb:
    """A successfully-parsed ``[SM] <verb>`` comment.

    ``verb`` — the matched :class:`Verbs` member.

    ``fields`` — parsed ``key=value`` arguments. The schema per verb
    is enforced by the dispatcher when it applies the verb; the
    parser only collects the raw fields.

    ``reason`` — convenience accessor for the common ``reason=``
    field. Many verbs carry it (``reject``, ``study-blocked``,
    ``design-revise``, ``continue``, ``unblock``, etc.).
    """

    verb: Verbs
    fields: dict[str, str]
    author: str

    @property
    def reason(self) -> str | None:
        return self.fields.get("reason")

    @property
    def findings(self) -> str | None:
        return self.fields.get("findings")

    @property
    def art_label(self) -> str | None:
        return self.fields.get("art")

    @property
    def note(self) -> str | None:
        return self.fields.get("note")


@dataclass(frozen=True)
class Continue(ParsedVerb):
    """Type alias for a ``[SM] continue`` parse.

    Exposed separately so type-narrowing in handler code is precise:
    ``if isinstance(parsed, Continue)`` reads cleaner than checking
    ``parsed.verb is Verbs.CONTINUE``.
    """


@dataclass(frozen=True)
class ParseError:
    """A ``[SM] `` comment that failed to parse.

    The dispatcher's responsibility: post ``reply_body`` as a comment
    on the issue (invariant I-6) and record the ``parse-error-reply``
    side-effect in the ledger with a 1-hour TTL to dedup repeat
    malformed attempts.
    """

    raw_body: str
    reason: str
    author: str | None

    @property
    def reply_body(self) -> str:
        """The auto-reply the dispatcher posts back on the issue."""
        return (
            f"[SM] parse-error reason=\"{self.reason}\"\n\n"
            f"Original body that failed to parse:\n"
            f"```\n{self.raw_body[:500]}\n```"
        )


# ----------------------------------------------------------------
# Parser
# ----------------------------------------------------------------


# ``key=value`` and ``key="quoted value"`` token grammar. Keys are
# alphanumeric + underscore + hyphen. Values are either quoted
# (allowing whitespace) or bare (no whitespace, no equals).
_KV_RE = re.compile(
    r'(?P<key>[A-Za-z_][A-Za-z0-9_-]*)='
    r'(?:"(?P<qval>[^"]*)"|(?P<bval>[^\s]+))'
)


def parse_comment(
    body: str,
    author: str | None,
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS_DEFAULT,
) -> ParsedVerb | ParseError | None:
    """Parse a GitHub comment body.

    Returns:

      * ``None`` if the body doesn't start with ``[SM] ``. Ordinary
        prose; the dispatcher ignores it.
      * :class:`ParsedVerb` (or :class:`Continue`) on a successful parse.
      * :class:`ParseError` if the body starts with ``[SM] `` but
        doesn't validate (unknown verb, untrusted author, malformed
        fields). The dispatcher posts the ``reply_body`` to the
        issue and records the parse-error in the ledger.

    Trust is enforced inside the parser: an untrusted author whose
    body otherwise parses still gets a :class:`ParseError` (with a
    "untrusted author" reason) so the failure is visible on the
    issue rather than silent.
    """
    if not body.startswith(f"{PREFIX} "):
        return None

    # Skip dispatcher self-audit comments — they carry the ``[SM] ``
    # prefix but are not transition verbs, so don't surface them as
    # parse errors. Handlers that need to detect specific audit
    # prefixes (e.g. v1 selected's orphan-design-ready recovery) do
    # so by raw ``body.startswith(...)`` checks separately.
    if is_audit_prefix(body):
        return None

    after_prefix = body[len(PREFIX) + 1 :].lstrip()
    if not after_prefix:
        return ParseError(
            raw_body=body,
            reason="empty [SM] comment — no verb after prefix",
            author=author,
        )

    # Split the verb off the rest. Verb is the first whitespace-
    # delimited token (or the whole tail if no whitespace).
    parts = after_prefix.split(None, 1)
    verb_token = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    # Resolve verb token to enum member. Unknown verbs produce a
    # parse-error reply so the operator sees the typo.
    verb = _resolve_verb(verb_token)
    if verb is None:
        return ParseError(
            raw_body=body,
            reason=f"unknown verb {verb_token!r}",
            author=author,
        )

    # Trust check — untrusted authors get a visible rejection.
    if author is None or author not in trusted_authors:
        return ParseError(
            raw_body=body,
            reason=(
                f"untrusted author {author!r}; only "
                f"{sorted(trusted_authors)} may drive {verb.value} transitions"
            ),
            author=author,
        )

    # Field extraction. Even on no-tail (bare `[SM] continue` for
    # example), the field dict is empty but still valid.
    fields = _extract_kv(tail)

    # Sanity: a bare reason field on a verb that doesn't accept one
    # is still parsed (we don't enforce per-verb field schemas at
    # parse time — the dispatcher applies them when matching against
    # the transitions table). Trailing tokens that aren't k=v shape
    # are silently dropped; this is the chosen relaxation that fixes
    # the v1 route-to-study trailing-prose bug. The body is still on
    # the issue thread, so context isn't lost.

    if verb is Verbs.CONTINUE:
        return Continue(verb=verb, fields=fields, author=author)
    return ParsedVerb(verb=verb, fields=fields, author=author)


def _resolve_verb(token: str) -> Verbs | None:
    """Map ``"route-to-study"`` → ``Verbs.ROUTE_TO_STUDY``, etc.

    Strict on the token: no case-folding, no aliases. Matches v1's
    parser strictness on the verb keyword itself — we relax only the
    trailing-content handling, not the verb name.
    """
    for v in Verbs:
        if v.value == token:
            return v
    return None


def _extract_kv(tail: str) -> dict[str, str]:
    """Pull every ``key=value`` token from ``tail`` into a dict.

    Quoted values keep their inner whitespace. Duplicate keys take
    the last occurrence. Tokens that don't match the kv shape are
    ignored (this is the trailing-content relaxation that fixes the
    v1 #300 bug).
    """
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(tail):
        key = m.group("key")
        if m.group("qval") is not None:
            out[key] = m.group("qval")
        else:
            out[key] = m.group("bval")
    return out
