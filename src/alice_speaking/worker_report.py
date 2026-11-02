"""Structured worker-report contract for background sub-agent dispatches.

Shape adopted from Jason's self-directed-agents test-to-completion kit
(https://github.com/jcronq/self-directed-agents). Four pieces live
here:

* :data:`WORKER_REPORT_PREAMBLE` — the instructions block the daemon
  prepends to every background sub-agent dispatch. Carries the
  "read PROJECT_PROFILE.md first" directive, the baseline-counts /
  reproduce-before-fix clauses (with the sub-30-LOC tactical carve-out
  per ``feedback_sm_tactical_threshold``), and the REQUIRED-REPORT
  trailing JSON schema the worker must emit at the end of its run.
* :func:`wrap_instructions` — prepend the preamble onto the raw
  instructions text the caller passed to the dispatch tool.
* :func:`parse_worker_report` / :func:`validate_worker_report` —
  extract the trailing JSON block from the worker's final text and
  check it against the required-field matrix (keyed on
  ``kind: code | config | research``).
* :func:`write_pending_verify_note` — drop a fleeting note into
  ``inner/notes/`` tagged ``pending-verify`` when the report is
  missing / invalid so Speaking's next turn can triage.

The preamble is intentionally textual-only — no templating, no
placeholders — so a worker reading the rendered prompt can see
exactly what it owes back. If the shape ever needs to drift per
dispatch (e.g. research-only tasks that genuinely can't produce a
commit SHA), the ``kind`` field in the trailing JSON is the escape
hatch rather than a conditional preamble.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("alice_speaking.worker_report")


# Sub-30-LOC code / sub-15-LOC config diffs are tactical per
# ``feedback_sm_tactical_threshold``; the worker is permitted to skip
# the baseline capture when the diff fits inside those bounds. The
# full-suite-after gate still applies — tactical != "no tests."
TACTICAL_CODE_LOC_THRESHOLD = 30
TACTICAL_CONFIG_LOC_THRESHOLD = 15


WORKER_REPORT_PREAMBLE = f"""\
# Worker protocol — read before starting

FIRST: read `PROJECT_PROFILE.md` at the top of the repo you're
working in. It tells you how to run tests, where the daemon lives,
how to deploy, which ports are in use, and which invariants are
non-negotiable. If the file does not exist, write a one-sentence
note at the end of your report flagging the gap and proceed with
best judgement.

## Baseline counts

Before changing code, run the test suite and record the pass / fail
/ errors / skipped counts as `baseline_suite` in the REQUIRED-REPORT
block below. The acceptance gate is `full_suite_after.failed == 0`
AND `full_suite_after.errors == 0`.

Tactical carve-out: for diffs under {TACTICAL_CODE_LOC_THRESHOLD}
LOC of code (or under {TACTICAL_CONFIG_LOC_THRESHOLD} LOC of config)
you may skip the baseline capture — but the full-suite-after gate
still applies. Set `baseline_suite: null` and note the LOC count in
`lessons` so the skip is auditable.

## Reproduce before fix

If you're fixing a live-site finding (a bug observed in prod / smoke
/ a dispatched turn in the logs), reproduce it first with a failing
test or a captured log line. Cite that artifact in your report
(path or log excerpt) before writing the fix. "I ran the code and
it fixed itself" is not evidence; a red test turning green is.

## REQUIRED-REPORT (trailing JSON block)

End your final assistant message with a single fenced JSON block
tagged ``json`` carrying exactly this shape. Nothing else may follow
it. Non-code tasks (research, config-only) can omit `commit_sha` /
`pr_url` / suite counts; `kind` keys the enforcement.

```json
{{
  "kind": "code",
  "branch": "feat/example-branch",
  "commit_sha": "0123456789abcdef...",
  "pr_url": "https://github.com/jcronq/alice/pull/123",
  "baseline_suite": {{
    "passed": 0, "failed": 0, "errors": 0, "skipped": 0
  }},
  "full_suite_after": {{
    "passed": 0, "failed": 0, "errors": 0, "skipped": 0
  }},
  "live_verified": "URL or filesystem path + what was checked",
  "amendments_addressed": ["one amendment per entry"],
  "unresolved": ["anything you didn't finish or had to defer"],
  "lessons": ["one sentence per lesson worth preserving"]
}}
```

Allowed `kind` values: `code` (required: `commit_sha`,
`full_suite_after`), `config` (required: `commit_sha`,
`full_suite_after`), `research` (no `commit_sha` / suite counts
required — `live_verified` points to the vault path you wrote).

If a required field genuinely does not apply, write `null` and
explain in `lessons`. Do NOT invent values — the parent turn checks
the report and will surface a pending-verify note if it looks
synthesised.
"""


# ---------------------------------------------------------------------------
# Instructions wrapper


def wrap_instructions(instructions: str) -> str:
    """Prepend :data:`WORKER_REPORT_PREAMBLE` to the caller-supplied
    sub-agent instructions.

    The preamble is deliberately prepended (not appended) so the
    worker reads the protocol before it reads the task. Idempotent on
    the exact preamble string so a caller that already wrapped doesn't
    double it.
    """
    text = instructions or ""
    if WORKER_REPORT_PREAMBLE in text:
        return text
    return f"{WORKER_REPORT_PREAMBLE}\n\n---\n\n{text}"


# ---------------------------------------------------------------------------
# Trailing JSON block parsing


# Opening-fence marker. We anchor to the LAST occurrence (via
# ``rfind`` in :func:`parse_worker_report`) rather than a regex with
# ``\Z``, because ``.*?`` would still backtrack to the EARLIEST
# opening fence and swallow interstitial content when the worker
# quoted an intermediate ``json`` sample.
_JSON_FENCE_OPEN = "```json"
_JSON_FENCE_CLOSE = "```"


def parse_worker_report(text: str) -> Optional[dict[str, Any]]:
    """Extract the trailing ``json`` fenced block from ``text`` and
    return it as a dict. Returns ``None`` if no block is present or
    the block isn't valid JSON.

    We intentionally only look at the LAST fenced block. A worker that
    emits an intermediate JSON code sample (quoting a payload from the
    issue, for example) still has to put the actual report at the end.
    """
    if not text:
        return None
    stripped = text.rstrip()
    open_idx = stripped.rfind(_JSON_FENCE_OPEN)
    if open_idx == -1:
        return None
    # Body starts after the first newline following the fence tag so
    # we tolerate ``\n`` or ``\r\n`` between the fence and the JSON.
    body_start = stripped.find("\n", open_idx + len(_JSON_FENCE_OPEN))
    if body_start == -1:
        return None
    body_start += 1
    close_idx = stripped.rfind(_JSON_FENCE_CLOSE)
    if close_idx <= body_start:
        return None
    body = stripped[body_start:close_idx].strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# Required fields per ``kind``. Keyed by the ``kind`` value in the
# report; the empty-string / unknown-kind case falls through to the
# strictest shape (code) so a worker that forgot to set ``kind``
# gets the most informative error list.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "code": ("commit_sha", "full_suite_after"),
    "config": ("commit_sha", "full_suite_after"),
    "research": ("live_verified",),
}


_SUITE_SUBFIELDS: tuple[str, ...] = ("passed", "failed", "errors", "skipped")


def validate_worker_report(report: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation issues against
    :func:`parse_worker_report`'s output. Empty list == report is
    acceptable for its declared ``kind``.

    Enforcement is intentionally lenient: it demands the structural
    shape ("if you said code, I need commit_sha + full_suite_after")
    but does not police the content ("is this SHA real on origin?").
    Live correctness is a Speaking-side triage decision; the parser
    only guards against workers that said "done" without evidence.
    """
    issues: list[str] = []
    kind = report.get("kind")
    if kind not in _REQUIRED_FIELDS:
        issues.append(
            f"kind must be one of {sorted(_REQUIRED_FIELDS)}; got {kind!r}"
        )
        required = _REQUIRED_FIELDS["code"]
    else:
        required = _REQUIRED_FIELDS[kind]

    for field in required:
        value = report.get(field)
        if value in (None, "", [], {}):
            issues.append(f"{field} is required for kind={kind!r} but is empty")

    # Suite-shape checks apply whenever the field is present AND the
    # kind actually requires it. A worker that explicitly set a
    # research kind can omit suites entirely; a code/config kind with
    # a malformed suite block is a real error.
    for field in ("baseline_suite", "full_suite_after"):
        suite = report.get(field)
        if suite is None:
            continue
        if not isinstance(suite, dict):
            issues.append(f"{field} must be an object; got {type(suite).__name__}")
            continue
        for sub in _SUITE_SUBFIELDS:
            if sub not in suite:
                issues.append(f"{field}.{sub} is missing")
            elif not isinstance(suite[sub], int):
                issues.append(
                    f"{field}.{sub} must be an integer; "
                    f"got {type(suite[sub]).__name__}"
                )

    return issues


# ---------------------------------------------------------------------------
# Done-without-evidence heuristic (optional second-tier check)
#
# This fires when the structured parse already succeeded OR failed —
# it's a prose-level sanity check for workers that claim completion
# without providing a commit / PR / test counts. The matcher is kept
# here (not in a separate module) so a single place owns the
# "did the worker provide evidence of what it says it did" question.


_CLAIM_DONE_RE = re.compile(
    r"\b(done|merged|fixed|complete|completed|landed)\b",
    re.IGNORECASE,
)


_EVIDENCE_RE = re.compile(
    r"(?:"
    r"commit\s+[0-9a-f]{7,40}"
    r"|PR\s*#\d+"
    r"|pr_url"
    r"|full_suite_after"
    r"|tests?.{0,20}(\d+).{0,20}(pass|green)"
    r")",
    re.IGNORECASE,
)


def looks_like_done_without_evidence(text: str) -> bool:
    """Return True when the worker's final text claims completion
    (``done`` / ``merged`` / ``fixed`` / ``complete``) but doesn't
    cite a commit SHA, PR number, structured report field, or
    passing-test count.

    Used as a secondary check after :func:`validate_worker_report`.
    Separated into its own function so the Stop-evidence tightening
    (self-directed-agents item 3) can be toggled independently of
    the primary structured-report contract.
    """
    if not text:
        return False
    if _CLAIM_DONE_RE.search(text) is None:
        return False
    if _EVIDENCE_RE.search(text) is not None:
        return False
    return True


# ---------------------------------------------------------------------------
# pending-verify note


def _slugify(s: str, max_len: int = 40) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return (s or "untitled")[:max_len]


def _stamp_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def write_pending_verify_note(
    mind_dir: Path,
    *,
    handle: str,
    description: str,
    reasons: list[str],
    result_excerpt: str,
) -> Optional[Path]:
    """Drop a fleeting note into ``mind_dir/inner/notes/`` tagged
    ``pending-verify`` so Speaking's next turn can triage.

    Mirrors :func:`alice_speaking.tools.inner.append_note` frontmatter
    shape (surface_dedup / gh-watcher metadata extraction depend on
    it). Returns the note path, or ``None`` if the write failed —
    callers should log but not raise, because losing a pending-verify
    note mustn't crash the completion-event handler.
    """
    if mind_dir is None:
        return None
    try:
        notes_dir = mind_dir / "inner" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(f"pending-verify-{handle}-{description}")
        path = notes_dir / f"{_stamp_utc()}-{slug}.md"
        created = (
            datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        )
        title = f"pending-verify: {description}".replace('"', "'")
        header_lines = [
            "---",
            f'title: "{title[:160]}"',
            "tag: pending-verify",
            f"created: {created}",
            "note_type: fleeting",
            "---",
            "",
            f"Background sub-agent `{handle}` returned without a usable "
            f"structured report.",
            "",
            "## Why this fired",
            "",
        ]
        header_lines.extend(f"- {reason}" for reason in reasons)
        truncated = (result_excerpt or "")[:1400].rstrip()
        header_lines.extend(
            [
                "",
                "## Final-text excerpt",
                "",
                "```",
                truncated,
                "```",
                "",
                "Speaking: triage the dispatched work, confirm the actual "
                "state (commit / PR / tests), then resolve this note.",
                "",
            ]
        )
        path.write_text("\n".join(header_lines))
        return path
    except OSError:
        log.exception(
            "write_pending_verify_note: failed to drop note for handle=%s",
            handle,
        )
        return None
