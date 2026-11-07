"""Self-merge policy — code-review gate before auto-merging a PR.

Issue #784 Part A. Today's self-merge policy was implicit (worker opens
PR → CI green → worker invokes ``gh pr merge``) and trusted CI alone.
CI catches syntax/type/test regressions but misses the class of bugs a
reading pass catches: dead code, off-by-one, subtle regressions, a11y
PRs that break something adjacent. This module centralises the gate so
every caller gets the same decision tree.

Policy
------

Given a PR that's open, CI-green, and authored by Alice (or jcronq),
the gate runs a ``/code-review`` agent at :data:`SelfMergeConfig.review_effort`
and classifies the resulting findings:

* **No findings** → merge.
* **Findings, not hot-path, auto_apply_findings=True** → apply the
  fixes via ``/code-review --fix``, wait for CI to re-green, then merge.
* **Findings AND hot-path** (auth/, migrations/, schema/*, *.sql) →
  block the merge, leave the findings as a PR comment, surface to
  Jason for review.
* **Findings but auto_apply_findings=False** → same as hot-path:
  block + surface.
* **Review timed out** (> :data:`SelfMergeConfig.review_timeout_seconds`)
  → log + proceed with merge anyway (don't block the pipeline on a
  stuck agent).

This module is a pure-Python policy library. The actual ``gh pr merge``
call and the ``/code-review`` subprocess spawn are injected as
callables so tests can mock them and so the worker scripts can wire
their own runtime in. The CLI wrapper ``bin/alice-self-merge`` is the
canonical entrypoint.

Config
------

Lives under ``speaking.self_merge`` in ``alice.config.json``::

    "self_merge": {
      "require_agent_review": true,
      "review_effort": "low",
      "auto_apply_findings": true,
      "review_timeout_seconds": 300
    }

Defaults are in :data:`SELF_MERGE_DEFAULTS`. ``require_agent_review:
false`` disables the gate entirely (pre-#784 behaviour).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


#: Default self-merge config. Mirrored under ``speaking.self_merge`` in
#: the fallback :data:`SPEAKING_DEFAULTS` dict of the infra config; the
#: dispatcher loader deep-merges user overrides on top.
SELF_MERGE_DEFAULTS: dict[str, Any] = {
    "require_agent_review": True,
    "review_effort": "low",
    "auto_apply_findings": True,
    "review_timeout_seconds": 300,
}


_VALID_EFFORTS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)


@dataclass(frozen=True)
class SelfMergeConfig:
    """Per-PR knobs for the self-merge gate."""

    require_agent_review: bool = True
    review_effort: str = "low"
    auto_apply_findings: bool = True
    review_timeout_seconds: int = 300

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SelfMergeConfig":
        """Build a config from a dict (typically ``speaking["self_merge"]``).

        Unknown keys are ignored. Unknown ``review_effort`` values are
        clamped to ``"low"`` with a warning so a typo can't fail-open
        at a higher effort than intended.
        """
        raw = dict(raw or {})
        effort = str(raw.get("review_effort") or SELF_MERGE_DEFAULTS["review_effort"])
        if effort not in _VALID_EFFORTS:
            log.warning(
                "self_merge.review_effort=%r not in %s — clamping to 'low'",
                effort,
                sorted(_VALID_EFFORTS),
            )
            effort = "low"
        try:
            timeout = int(raw.get("review_timeout_seconds", SELF_MERGE_DEFAULTS["review_timeout_seconds"]))
        except (TypeError, ValueError):
            timeout = int(SELF_MERGE_DEFAULTS["review_timeout_seconds"])
        if timeout < 1:
            timeout = int(SELF_MERGE_DEFAULTS["review_timeout_seconds"])
        return cls(
            require_agent_review=bool(
                raw.get("require_agent_review", SELF_MERGE_DEFAULTS["require_agent_review"])
            ),
            review_effort=effort,
            auto_apply_findings=bool(
                raw.get("auto_apply_findings", SELF_MERGE_DEFAULTS["auto_apply_findings"])
            ),
            review_timeout_seconds=timeout,
        )


# ---------------------------------------------------------------------------
# Hot-path classifier
# ---------------------------------------------------------------------------


#: Files matching any of these regexes are "hot-path": auth, DB
#: migrations, schema definitions, or raw SQL. A finding on any of
#: these blocks auto-apply + merge even when
#: ``auto_apply_findings=True``; the gate escalates to Jason instead.
#:
#: Kept intentionally narrow. Hot-path = "a bad diff here affects
#: production state, not just UX" — not "important code". The broader
#: "important" surface is checked by the review itself.
HOT_PATH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        r"(^|/)auth(/|$|\.)",
        r"(^|/)migrations?/",
        r"(^|/)alembic/",
        r"(^|/)schema(/|$|\.)",
        r"\.sql$",
        r"(^|/)credentials",
        r"(^|/)secrets?/",
    )
)


def is_hot_path(files: list[str]) -> bool:
    """Return True iff any file in ``files`` matches a hot-path pattern.

    An empty file list returns False (nothing to touch).
    """
    for path in files:
        if not isinstance(path, str) or not path:
            continue
        for pat in HOT_PATH_PATTERNS:
            if pat.search(path):
                return True
    return False


# ---------------------------------------------------------------------------
# Review result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewFinding:
    """One high-signal finding from the ``/code-review`` agent."""

    file: str
    summary: str
    short_summary: str = ""
    line: Optional[int] = None
    category: str = ""


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of a ``/code-review`` run."""

    findings: tuple[ReviewFinding, ...] = ()
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return not self.findings and not self.timed_out and self.error is None


# ---------------------------------------------------------------------------
# /code-review subprocess runner
# ---------------------------------------------------------------------------


def run_code_review_agent(
    repo: str,
    pr_number: int,
    *,
    effort: str = "low",
    timeout_seconds: int = 300,
    claude_bin: str = "claude",
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] | None = None,
) -> ReviewResult:
    """Spawn a one-shot ``claude -p "/code-review ..."`` process and
    parse its structured findings out of stdout.

    The ``/code-review`` skill (see
    ``~/.claude/plugins/.../code-review/commands/code-review.md``)
    reports via the ``ReportFindings`` tool. We invoke with
    ``--max-findings`` enforced and parse the JSON list out of the
    harness's stdout.

    ``runner`` is injected by tests; the default shells out via
    :mod:`subprocess`.
    """
    cmd = [
        claude_bin,
        "-p",
        f"/code-review {repo}#{pr_number} {effort} --max-findings 20",
    ]
    _runner = runner or _default_runner
    try:
        result = _runner(cmd, timeout_seconds)
    except subprocess.TimeoutExpired:
        log.warning(
            "self_merge: /code-review timed out after %ds on %s#%d",
            timeout_seconds,
            repo,
            pr_number,
        )
        return ReviewResult(timed_out=True)
    except Exception as exc:  # noqa: BLE001 — bubble up as structured error
        return ReviewResult(error=f"{type(exc).__name__}: {exc}")

    if result.returncode != 0:
        return ReviewResult(
            error=(
                f"claude exited {result.returncode}: "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
        )

    findings = _parse_findings(result.stdout or "")
    return ReviewResult(findings=tuple(findings))


def _default_runner(
    cmd: list[str], timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    """Default subprocess runner. Tests patch :func:`run_code_review_agent`
    with their own callable instead."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _iter_json_object_candidates(text: str) -> "list[str]":
    """Walk ``text`` and return every ``{...}`` substring that balances
    braces (ignoring braces inside string literals).

    ``json.loads`` is applied by the caller; this function only
    enumerates candidates. Nested-object cases return the outermost
    span for each top-level ``{`` so a payload like
    ``{"findings":[{...}]}`` is captured in one piece.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        start = i
        in_str = False
        escape = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(text[start : j + 1])
                        break
            j += 1
        i = j + 1 if j < n else n
    return out


def _parse_findings(stdout: str) -> list[ReviewFinding]:
    """Extract structured findings from a ``/code-review`` run's stdout.

    The skill reports via the host's ``ReportFindings`` tool which
    emits a JSON payload like::

        {"findings": [{"file": "...", "summary": "...", ...}, ...]}

    The harness pretty-prints that around tool-use frames. We greedily
    pick the last payload containing a ``findings`` array — the
    reporting call comes after any in-flight exploration. Returns an
    empty list when no payload matches (the ``No issues found``
    terminal path).
    """
    out: list[ReviewFinding] = []
    best: dict[str, Any] | None = None
    for chunk in _iter_json_object_candidates(stdout):
        if "\"findings\"" not in chunk:
            continue
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        items = payload.get("findings")
        if not isinstance(items, list):
            continue
        best = payload
    if best is None:
        return out
    for item in best.get("findings") or []:
        if not isinstance(item, dict):
            continue
        file_ = str(item.get("file") or "")
        summary = str(item.get("summary") or "")
        if not file_ or not summary:
            continue
        line_raw = item.get("line")
        line_val: Optional[int]
        try:
            line_val = int(line_raw) if line_raw is not None else None
        except (TypeError, ValueError):
            line_val = None
        out.append(
            ReviewFinding(
                file=file_,
                summary=summary,
                short_summary=str(item.get("short_summary") or ""),
                line=line_val,
                category=str(item.get("category") or ""),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Decision tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeDecision:
    """Outcome of :func:`decide_self_merge`.

    ``action`` is one of:

    * ``"merge"`` — the gate cleared; caller should run ``gh pr merge``.
    * ``"merge_after_fix"`` — findings exist but are low-risk; caller
      should apply the findings via ``/code-review --fix``, wait for
      CI to re-green, then merge.
    * ``"block"`` — findings require human eyes; caller should NOT
      merge, should post findings as a PR comment and surface to Jason.
    * ``"merge_review_disabled"`` — ``require_agent_review=False``;
      the gate is a no-op and the caller should merge.
    """

    action: str
    reason: str
    findings: tuple[ReviewFinding, ...] = ()
    hot_path_hit: bool = False
    timed_out: bool = False


def decide_self_merge(
    *,
    repo: str,
    pr_number: int,
    pr_files: list[str],
    config: SelfMergeConfig,
    run_review: Callable[..., ReviewResult] = run_code_review_agent,
) -> MergeDecision:
    """Apply the self-merge policy to one PR and return the decision.

    The caller is responsible for the preconditions (``PR open +
    CI-green + trusted author``) and for acting on the decision — this
    function does not touch GitHub itself. The ``run_review`` callable
    is injected so tests can mock it; production wiring uses the
    module-level :func:`run_code_review_agent`.
    """
    if not config.require_agent_review:
        return MergeDecision(
            action="merge_review_disabled",
            reason="self_merge.require_agent_review=False — gate skipped",
        )

    result = run_review(
        repo,
        pr_number,
        effort=config.review_effort,
        timeout_seconds=config.review_timeout_seconds,
    )

    if result.timed_out:
        return MergeDecision(
            action="merge",
            reason=(
                f"/code-review timed out after {config.review_timeout_seconds}s "
                f"— proceeding with merge per policy (don't block the pipeline)"
            ),
            timed_out=True,
        )

    if result.error:
        return MergeDecision(
            action="block",
            reason=f"/code-review failed to run: {result.error}",
        )

    if not result.findings:
        return MergeDecision(
            action="merge",
            reason="/code-review returned zero findings",
        )

    # Findings present. Decide between auto-fix and escalation.
    touched_files = list(pr_files) + [f.file for f in result.findings if f.file]
    hot = is_hot_path(touched_files)

    if hot:
        return MergeDecision(
            action="block",
            reason=(
                f"{len(result.findings)} finding(s) on a hot-path diff "
                f"(auth/migrations/schema/sql/credentials); human review required"
            ),
            findings=result.findings,
            hot_path_hit=True,
        )

    if not config.auto_apply_findings:
        return MergeDecision(
            action="block",
            reason=(
                f"{len(result.findings)} finding(s); "
                f"self_merge.auto_apply_findings=False, human review required"
            ),
            findings=result.findings,
        )

    return MergeDecision(
        action="merge_after_fix",
        reason=(
            f"{len(result.findings)} low-risk finding(s); "
            f"apply via /code-review --fix, re-green CI, then merge"
        ),
        findings=result.findings,
    )


# ---------------------------------------------------------------------------
# PR → item linking (#828, PR-5)
# ---------------------------------------------------------------------------


def record_pr_item_ref(
    *,
    conn: sqlite3.Connection,
    repo: str,
    pr_body: str | None,
    pr_url: str,
    actor: str = "speaking",
    now: datetime | None = None,
) -> str | None:
    """Extract ``Closes item-<N>``, resolve to the canonical item_id,
    and record it via :func:`alice_forge.sm.store.record_pr_ref`.

    Returns the canonical item_id written, or ``None`` if the PR body
    had no ``Closes item-<N>`` line (logged as a warning — every
    worker-authored PR body should carry one post-PR-5, so a miss is
    a signal the prompt template drifted, not an expected path).
    Best-effort: this must never be the reason a PR fails to merge,
    so callers should not let an exception here abort the merge flow.
    """
    from watchers.github import extract_closes_item_ref
    from alice_state_db.sm_seed import gh_issue_id
    from alice_forge.sm import store

    extracted = extract_closes_item_ref(pr_body, pr_url)
    if extracted is None:
        log.warning("no `Closes item-<N>` line in PR body for %s", pr_url)
        return None
    alias, _ = extracted
    n = int(alias.removeprefix("item-"))
    item_id = gh_issue_id(repo, n)
    store.record_pr_ref(
        conn=conn, item_id=item_id, pr_url=pr_url, actor=actor, now=now
    )
    return item_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> "argparse.ArgumentParser":  # type: ignore[name-defined]  # noqa: F821  (local-import keeps module-level deps light)
    import argparse

    parser = argparse.ArgumentParser(
        prog="alice-self-merge",
        description=(
            "Decide whether a PR is safe to self-merge after a "
            "/code-review gate. Prints a single JSON line with the "
            "decision; exits 0 when the decision is a merge-action, "
            "2 when the merge is blocked, 1 on hard error."
        ),
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="<user>/<repo> slug, e.g. jcronq/alice",
    )
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--files",
        action="append",
        default=[],
        help=(
            "Changed file path (repeatable). The caller is expected to "
            "pass `gh pr diff --name-only ...` output."
        ),
    )
    parser.add_argument(
        "--effort",
        default=SELF_MERGE_DEFAULTS["review_effort"],
        choices=sorted(_VALID_EFFORTS),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=SELF_MERGE_DEFAULTS["review_timeout_seconds"],
    )
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="Disable the gate (sets require_agent_review=False).",
    )
    parser.add_argument(
        "--no-auto-apply",
        action="store_true",
        help="Disable auto-apply of findings (sets auto_apply_findings=False).",
    )
    # --- PR → item linking (#828, PR-5) ---------------------------
    # Invocable today so the plumbing can be exercised from scripts
    # even though the runtime caller (speaking-shim PhaseRunner) is
    # still a stub. When all three flags are supplied, the merge
    # decision still runs — the ref recording is additive and errors
    # inside it are swallowed so they can never block a merge.
    parser.add_argument(
        "--record-item-ref-db",
        default=None,
        help=(
            "Path to alice-state.db. When set, extract `Closes "
            "item-<N>` from --pr-body-file and record a gh:pr "
            "reference row in item_external_refs before deciding "
            "the merge."
        ),
    )
    parser.add_argument(
        "--pr-url",
        default=None,
        help="Full PR URL (required with --record-item-ref-db).",
    )
    parser.add_argument(
        "--pr-body-file",
        default=None,
        help=(
            "Path to a file containing the PR body, or `-` to read "
            "from stdin (required with --record-item-ref-db)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Prints a JSON decision line to stdout.

    Exit codes:

    * ``0`` — merge or merge_after_fix (caller proceeds)
    * ``2`` — block (caller must NOT merge)
    * ``1`` — hard error (unexpected exception)
    """
    import argparse  # noqa: F401 — used via the local import in _build_argparser

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    config = SelfMergeConfig(
        require_agent_review=not args.no_review,
        review_effort=args.effort,
        auto_apply_findings=not args.no_auto_apply,
        review_timeout_seconds=args.timeout,
    )
    # --- PR → item linking side-effect (#828, PR-5) ---------------
    # Best-effort: an exception here must never abort merge
    # decisioning, so the whole block is wrapped. Missing flags are
    # silently ignored; present-but-partial flags log + continue.
    if args.record_item_ref_db:
        try:
            if not args.pr_url or not args.pr_body_file:
                log.warning(
                    "--record-item-ref-db requires --pr-url and --pr-body-file; "
                    "skipping item-ref record for repo=%s pr=%s",
                    args.repo,
                    args.pr,
                )
            else:
                import sys as _sys
                from alice_state_db.schema import apply_schema

                if args.pr_body_file == "-":
                    pr_body = _sys.stdin.read()
                else:
                    with open(args.pr_body_file, "r", encoding="utf-8") as fh:
                        pr_body = fh.read()
                _ref_conn = sqlite3.connect(args.record_item_ref_db)
                try:
                    _ref_conn.execute("PRAGMA foreign_keys = ON")
                    apply_schema(_ref_conn)
                    recorded = record_pr_item_ref(
                        conn=_ref_conn,
                        repo=args.repo,
                        pr_body=pr_body,
                        pr_url=args.pr_url,
                        actor="speaking",
                    )
                finally:
                    _ref_conn.close()
                print(json.dumps({"item_id": recorded}), flush=True)
        except Exception as exc:  # noqa: BLE001 — never abort merge
            log.exception(
                "record-item-ref failed for repo=%s pr_url=%s: %s",
                args.repo,
                args.pr_url,
                exc,
            )
    try:
        decision = decide_self_merge(
            repo=args.repo,
            pr_number=args.pr,
            pr_files=list(args.files or []),
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 — surface and exit
        print(
            json.dumps({"action": "error", "reason": f"{type(exc).__name__}: {exc}"}),
            flush=True,
        )
        return 1
    payload = {
        "action": decision.action,
        "reason": decision.reason,
        "hot_path_hit": decision.hot_path_hit,
        "timed_out": decision.timed_out,
        "findings": [
            {
                "file": f.file,
                "summary": f.summary,
                "short_summary": f.short_summary,
                "line": f.line,
                "category": f.category,
            }
            for f in decision.findings
        ],
    }
    print(json.dumps(payload), flush=True)
    if decision.action == "block":
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main(sys.argv[1:]))
