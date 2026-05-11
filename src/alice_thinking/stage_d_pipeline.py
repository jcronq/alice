"""Stage D dual-judge pipeline — orchestrates one synthesis attempt loop.

Wraps :func:`alice_thinking.stage_d_judges.judge_qwen` /
:func:`~alice_thinking.stage_d_judges.judge_haiku` into the agreement +
reassess protocol from
``cortex-memory/research/2026-05-08-stage-d-dual-judge-protocol.md`` (§2.3)
and the firehose-logging directive from
``cortex-memory/research/2026-05-09-stage-d-labeling-pipeline.md`` (§8).

Outcomes (mirrors :file:`src/alice_viewer/STAGE_D_SCHEMA.md`):

- ``shipped`` — both judges agree ``ship`` on some attempt.
- ``dropped_agreement_reject`` — both judges agree ``reject`` on the
  first attempt. We do NOT keep regenerating after agreement-reject —
  the synthesis is genuinely bad on this pair.
- ``dropped_disagreement_exhausted`` — disagreement persists through
  ``max_attempts`` (default 3); pair logged, vault unchanged.
- ``disagreement_pending`` — used only on the in-flight JSONL line
  emitted at each disagreement attempt before re-drafting; the final
  record on a successful pipeline run will never be ``disagreement_pending``.

JSONL contract:

- One line is appended to the attempts log **per judge attempt**, not
  just per pipeline run. Firehose, not sampling.
- File is opened ``mode='a'``, line written + flushed, file closed.
  Append-on-line is atomic for typical line sizes (< pipe buffer).
- Schema matches :file:`src/alice_viewer/STAGE_D_SCHEMA.md` field-for-field.
- ``shipped_slug`` is ``None`` when the pipeline returns; the caller
  writes the vault note and is expected to update the most recent JSONL
  line with the slug. See :func:`update_shipped_slug` for the helper.

The pipeline never writes the vault note itself — that's still the
caller's responsibility (Stage D wake prompt). The pipeline only:

1. invokes both judges on the candidate synthesis,
2. on disagreement, calls back into ``draft_synthesis_fn`` for a fresh
   draft and re-runs both judges,
3. logs every attempt to the JSONL queue, and
4. returns a typed :class:`AttemptRecord` describing the outcome.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional


__all__ = [
    "AttemptRecord",
    "CommitResult",
    "CommitOutcome",
    "Outcome",
    "DEFAULT_ATTEMPTS_LOG",
    "DEFAULT_JUDGE_FAILURES_LOG",
    "DEFAULT_VAULT_ROOT",
    "commit_stage_d_synthesis",
    "run_dual_judge",
    "update_shipped_slug",
]


Outcome = Literal[
    "shipped",
    "dropped_agreement_reject",
    "dropped_disagreement_exhausted",
    "disagreement_pending",
]

# Commit-level outcome adds ``fallback`` for the judge-unreachable path
# where ``commit_stage_d_synthesis`` writes the vault note without judge
# verdicts (and logs the failure to ``stage-d-judge-failures.jsonl``).
CommitOutcome = Literal[
    "shipped",
    "dropped_agreement_reject",
    "dropped_disagreement_exhausted",
    "fallback",
]


DEFAULT_ATTEMPTS_LOG = pathlib.Path.home() / "alice-mind/inner/state/stage-d-attempts.jsonl"
DEFAULT_JUDGE_FAILURES_LOG = (
    pathlib.Path.home() / "alice-mind/inner/state/stage-d-judge-failures.jsonl"
)
DEFAULT_VAULT_ROOT = pathlib.Path.home() / "alice-mind/cortex-memory"


@dataclass
class AttemptRecord:
    """One Stage D pipeline run's final state.

    Fields match :file:`src/alice_viewer/STAGE_D_SCHEMA.md`. The pipeline
    writes one JSONL line per attempt during the run; this record
    represents the final run-level state the caller sees.
    """

    id: str
    pair: dict  # {"slug_a": ..., "slug_b": ...}
    synthesis_text: str
    draft_attempt_n: int
    qwen_verdict: Optional[dict]
    haiku_verdict: Optional[dict]
    outcome: Outcome
    retry_history: list[str] = field(default_factory=list)
    created_at: str = ""
    shipped_slug: Optional[str] = None

    def to_jsonl_dict(self) -> dict:
        """Return the dict shape that goes on the JSONL line. ``pair``
        and verdict dicts are emitted as-is — they already match the
        schema."""
        return {
            "id": self.id,
            "pair": self.pair,
            "synthesis_text": self.synthesis_text,
            "draft_attempt_n": self.draft_attempt_n,
            "qwen_verdict": self.qwen_verdict,
            "haiku_verdict": self.haiku_verdict,
            "outcome": self.outcome,
            "retry_history": list(self.retry_history),
            "created_at": self.created_at,
            "shipped_slug": self.shipped_slug,
        }


def _now_iso() -> str:
    """Local time with offset, ISO 8601. Matches the schema example
    (``2026-05-09T03:14:22-04:00``)."""
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _new_attempt_id() -> str:
    """uuid4 hex — schema permits any unique string. Wake prompt expects
    nothing in particular; viewer treats it as opaque."""
    return f"att-{uuid.uuid4().hex[:24]}"


def _atomic_append_jsonl(path: pathlib.Path, record: dict) -> None:
    """Append one JSONL line, flush, close. Creates parent dirs as
    needed. Per-line append is atomic for typical sizes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
        fh.flush()


def run_dual_judge(
    *,
    slug_a: str,
    slug_b: str,
    source_a_text: str,
    source_b_text: str,
    draft_synthesis_fn: Callable[[Optional[str]], str],
    prior_pair_synthesis: Optional[str] = None,
    attempts_log_path: pathlib.Path = DEFAULT_ATTEMPTS_LOG,
    max_attempts: int = 3,
    judge_qwen_fn: Optional[Callable[..., dict]] = None,
    judge_haiku_fn: Optional[Callable[..., dict]] = None,
) -> AttemptRecord:
    """Run the dual-judge protocol for one candidate pair.

    Parameters
    ----------
    slug_a / slug_b
        Vault slugs of the two source notes (logged under ``pair``).
    source_a_text / source_b_text
        Full body text of each source — passed to both judges.
    draft_synthesis_fn
        Callable invoked to produce each synthesis draft. Called with
        ``None`` on the first attempt; on subsequent attempts (after
        disagreement) called with the prior draft text so the synthesizer
        can address the disagreeing judge's concern. Must return a
        non-empty string.
    prior_pair_synthesis
        Body text of any previously-shipped synthesis for this exact
        pair. Used by both judges' novelty checks; ``None`` means first
        synthesis attempt on this pair.
    attempts_log_path
        Where to append per-attempt JSONL lines. Defaults to
        ``~/alice-mind/inner/state/stage-d-attempts.jsonl``.
    max_attempts
        Total drafts (original + reassesses). Spec default is 3.
    judge_qwen_fn / judge_haiku_fn
        Optional injection seams for testing. When ``None`` (production)
        the module-level :mod:`alice_thinking.stage_d_judges` callables
        are used.

    Returns
    -------
    AttemptRecord
        The final state. Caller writes the vault note when
        ``outcome == "shipped"``, then calls :func:`update_shipped_slug`
        on ``attempts_log_path`` with the resulting vault slug.
    """
    # Lazy import — keeps test paths light. Tests typically pass
    # judge_*_fn directly, so neither import is forced.
    if judge_qwen_fn is None or judge_haiku_fn is None:
        from alice_thinking import stage_d_judges as _sj
        if judge_qwen_fn is None:
            judge_qwen_fn = _sj.judge_qwen
        if judge_haiku_fn is None:
            judge_haiku_fn = _sj.judge_haiku

    attempt_id = _new_attempt_id()
    pair = {"slug_a": slug_a, "slug_b": slug_b}
    retry_history: list[str] = []
    last_synthesis: Optional[str] = None
    last_qwen: Optional[dict] = None
    last_haiku: Optional[dict] = None
    outcome: Outcome = "disagreement_pending"
    final_n = 0

    for n in range(1, max_attempts + 1):
        # Draft (or re-draft) the synthesis. First attempt: prior=None.
        # Subsequent attempts: pass the previous draft so the synthesizer
        # can rewrite it addressing the disagreement.
        synthesis = draft_synthesis_fn(last_synthesis)
        if not synthesis or not synthesis.strip():
            raise ValueError(
                f"draft_synthesis_fn returned empty synthesis on attempt {n}"
            )
        # Track all earlier drafts in retry_history (excludes the current).
        if last_synthesis is not None:
            retry_history.append(last_synthesis)

        last_synthesis = synthesis
        last_qwen = judge_qwen_fn(
            synthesis=synthesis,
            source_a_text=source_a_text,
            source_b_text=source_b_text,
            prior_pair_synthesis=prior_pair_synthesis,
        )
        last_haiku = judge_haiku_fn(
            synthesis=synthesis,
            source_a_text=source_a_text,
            source_b_text=source_b_text,
            prior_pair_synthesis=prior_pair_synthesis,
        )

        qwen_decision = last_qwen.get("decision")
        haiku_decision = last_haiku.get("decision")

        if qwen_decision == "ship" and haiku_decision == "ship":
            outcome = "shipped"
        elif qwen_decision == "reject" and haiku_decision == "reject":
            outcome = "dropped_agreement_reject"
        elif n >= max_attempts:
            outcome = "dropped_disagreement_exhausted"
        else:
            outcome = "disagreement_pending"

        final_n = n
        record = AttemptRecord(
            id=attempt_id,
            pair=pair,
            synthesis_text=synthesis,
            draft_attempt_n=n,
            qwen_verdict=dict(last_qwen) if last_qwen else None,
            haiku_verdict=dict(last_haiku) if last_haiku else None,
            outcome=outcome,
            retry_history=list(retry_history),
            created_at=_now_iso(),
            shipped_slug=None,
        )
        _atomic_append_jsonl(attempts_log_path, record.to_jsonl_dict())

        # Terminal outcomes break the loop.
        if outcome in ("shipped", "dropped_agreement_reject"):
            break
        if outcome == "dropped_disagreement_exhausted":
            break
        # disagreement_pending — loop again.

    return AttemptRecord(
        id=attempt_id,
        pair=pair,
        synthesis_text=last_synthesis or "",
        draft_attempt_n=final_n,
        qwen_verdict=dict(last_qwen) if last_qwen else None,
        haiku_verdict=dict(last_haiku) if last_haiku else None,
        outcome=outcome,
        retry_history=list(retry_history),
        created_at=_now_iso(),
        shipped_slug=None,
    )


def update_shipped_slug(
    *,
    attempt_id: str,
    shipped_slug: str,
    attempts_log_path: pathlib.Path = DEFAULT_ATTEMPTS_LOG,
) -> bool:
    """After the caller writes the vault note for a shipped synthesis,
    record the resulting slug on the most recent JSONL line for the
    given ``attempt_id``.

    Append-only contract: this writes a NEW JSONL line — the most recent
    line wins on viewer read by virtue of the in-process join. The
    receive-side viewer schema treats stage-d-labels as the
    last-write-wins sidecar; for the attempts log, the writer's contract
    is "one line per attempt, the line before the slug update is the
    canonical attempt record." Adding a slug-update line is a no-op for
    correctness because the viewer joins on ``attempt_id`` and reads the
    latest matching line for the shipped_slug field.

    For now, the simplest implementation is in-place: read all lines,
    rewrite the last line for this id with the slug filled in, write
    atomically via temp+rename. Append-only readers that pre-cache by
    line offset will need a refresh, but the viewer reads start-to-end
    on every request and is order-independent.

    Returns True if a line was updated, False if no matching line was
    found.
    """
    if not attempts_log_path.exists():
        return False

    lines: list[str] = attempts_log_path.read_text(encoding="utf-8").splitlines()
    last_match_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == attempt_id:
            last_match_idx = i
            break

    if last_match_idx == -1:
        return False

    obj = json.loads(lines[last_match_idx])
    obj["shipped_slug"] = shipped_slug
    lines[last_match_idx] = json.dumps(obj, ensure_ascii=False)

    tmp = attempts_log_path.with_suffix(attempts_log_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(attempts_log_path)
    return True


# ---------------------------------------------------------------------------
# commit_stage_d_synthesis — the deterministic single-call entry point.
#
# Collapses the wake's old steps 5 (judge) + 6 (vault write) + 7 (pairs
# log) into one function so thinking can't ship a Stage D synthesis
# without going through the judge gate. The prompt-level instruction
# was advisory and was being skipped — see the gap analysis on
# 2026-05-11. This function is the structural fix.


@dataclass
class CommitResult:
    """Outcome of one ``commit_stage_d_synthesis`` call.

    Fields
    ------
    outcome
        ``shipped`` — judges agreed ship; vault note written.
        ``dropped_agreement_reject`` — judges agreed reject; no vault write.
        ``dropped_disagreement_exhausted`` — disagreement through max_attempts; no vault write.
        ``fallback`` — judge dispatch raised; vault note written from the
        original draft and the failure is logged to
        ``stage-d-judge-failures.jsonl``.
    synthesis_slug
        Vault slug of the written note (``research/<slug>``), or ``None``
        when no vault note was written (both drop outcomes).
    attempt_id
        ``run_dual_judge`` attempt id, or ``None`` for the fallback path
        where the judge never ran.
    fallback_reason
        Short error string when ``outcome == "fallback"``; ``None`` otherwise.
    """

    outcome: CommitOutcome
    synthesis_slug: Optional[str]
    attempt_id: Optional[str]
    fallback_reason: Optional[str] = None


def _today_pairs_log_path() -> pathlib.Path:
    """Today's pairs log: ``inner/state/stage-d-pairs-YYYY-MM-DD.jsonl``."""
    today = _dt.datetime.now().astimezone().date().isoformat()
    return pathlib.Path.home() / f"alice-mind/inner/state/stage-d-pairs-{today}.jsonl"


def _vault_note_path(vault_root: pathlib.Path, output_slug: str) -> pathlib.Path:
    return vault_root / "research" / f"{output_slug}.md"


def commit_stage_d_synthesis(
    *,
    slug_a: str,
    slug_b: str,
    source_a_text: str,
    source_b_text: str,
    draft_synthesis: str,
    output_slug: str,
    note_content: str,
    prior_pair_synthesis: Optional[str] = None,
    vault_root: pathlib.Path = DEFAULT_VAULT_ROOT,
    pairs_log_path: Optional[pathlib.Path] = None,
    attempts_log_path: pathlib.Path = DEFAULT_ATTEMPTS_LOG,
    judge_failures_log_path: pathlib.Path = DEFAULT_JUDGE_FAILURES_LOG,
    max_attempts: int = 3,
    judge_qwen_fn: Optional[Callable[..., dict]] = None,
    judge_haiku_fn: Optional[Callable[..., dict]] = None,
) -> CommitResult:
    """Run the dual-judge gate, write the vault note on ship-or-fallback,
    and append the pairs log. Single-call entry point so the wake can't
    short-circuit any of these steps.

    Inputs
    ------
    slug_a, slug_b
        Source-note slugs. Caller is responsible for canonical ordering
        (alphabetically first goes in slug_a) — the pair-log dedup check
        elsewhere is order-independent but consistent ordering keeps
        downstream tooling simple.
    source_a_text, source_b_text
        Full body text of each source note, passed to both judges.
    draft_synthesis
        The candidate synthesis body that thinking has already drafted.
        The pipeline uses this as the input on every judge attempt; the
        synthesizer is thinking herself, so for the v1 pipeline we don't
        re-invoke a model — the 3-attempt loop will burn through and the
        disagreement will be visible in the JSONL log if the judges
        can't agree on the unchanged draft.
    output_slug
        Slug the vault note will be written under. Convention is
        ``YYYY-MM-DD-<short-slug>``; the file lands at
        ``<vault_root>/research/<output_slug>.md``. Caller picks.
    note_content
        Full markdown body (including frontmatter) that will be written
        to the vault file. Caller's responsibility to include the
        ``source: stage-d``, ``note_a``, and ``note_b`` frontmatter
        fields — see the wake prompt for the convention. The invariant
        check (separate utility) verifies these on read.
    prior_pair_synthesis
        Body of any previously-shipped synthesis on this exact pair, for
        the judges' novelty check. ``None`` for a fresh pair.
    vault_root
        Root of the cortex-memory vault. Defaults to ``~/alice-mind/cortex-memory``.
    pairs_log_path
        Override for the pairs log. Default: today's
        ``inner/state/stage-d-pairs-YYYY-MM-DD.jsonl`` (created on first
        append).
    attempts_log_path
        Forwarded to ``run_dual_judge`` (firehose attempt log).
    judge_failures_log_path
        Append target for the fallback path. One JSONL line per failure.
    max_attempts
        Forwarded to ``run_dual_judge``. Spec default 3.
    judge_qwen_fn, judge_haiku_fn
        Test-injection seams. ``None`` (default) → production callables
        from :mod:`alice_thinking.stage_d_judges`.

    Returns
    -------
    CommitResult
        See class docstring for field semantics.

    Side effects (in order)
    -----------------------
    1. Per-attempt JSONL lines appended to ``attempts_log_path`` (via
       ``run_dual_judge``).
    2. On judge-dispatch failure: one JSONL line appended to
       ``judge_failures_log_path``. No attempt lines (judge never ran).
    3. Vault note written to ``<vault_root>/research/<output_slug>.md``
       on ``shipped`` and ``fallback``. NOT written on drop outcomes.
    4. On ``shipped``: ``update_shipped_slug`` rewrites the most recent
       attempt JSONL line with the slug.
    5. One JSONL line appended to ``pairs_log_path`` with
       ``{"ts": ..., "note_a": slug_a, "note_b": slug_b, "synthesis": <slug-or-null>}``.
       The pairs log line is appended on EVERY commit (including drops)
       so the nightly cap and dedup logic stays consistent.
    """
    pairs_path = pairs_log_path or _today_pairs_log_path()
    vault_path = _vault_note_path(vault_root, output_slug)
    pair_slug = f"research/{output_slug}"

    # 1. Run the judge gate. Catch any exception as a fallback signal so
    # the buggy-judge-layer failure mode doesn't take Stage D offline.
    rec: Optional[AttemptRecord] = None
    fallback_reason: Optional[str] = None
    try:
        rec = run_dual_judge(
            slug_a=slug_a,
            slug_b=slug_b,
            source_a_text=source_a_text,
            source_b_text=source_b_text,
            draft_synthesis_fn=lambda _prior: draft_synthesis,
            prior_pair_synthesis=prior_pair_synthesis,
            attempts_log_path=attempts_log_path,
            max_attempts=max_attempts,
            judge_qwen_fn=judge_qwen_fn,
            judge_haiku_fn=judge_haiku_fn,
        )
    except Exception as exc:  # noqa: BLE001 — fallback intentionally catches anything
        fallback_reason = f"{type(exc).__name__}: {exc}"
        _atomic_append_jsonl(
            judge_failures_log_path,
            {
                "ts": _now_iso(),
                "slug_a": slug_a,
                "slug_b": slug_b,
                "reason": fallback_reason,
            },
        )

    # 2. Decide whether to write the vault note.
    commit_outcome: CommitOutcome
    synthesis_slug: Optional[str]
    attempt_id: Optional[str]

    if rec is None:
        commit_outcome = "fallback"
        synthesis_slug = pair_slug
        attempt_id = None
        _write_vault_note(vault_path, note_content)
    elif rec.outcome == "shipped":
        commit_outcome = "shipped"
        synthesis_slug = pair_slug
        attempt_id = rec.id
        _write_vault_note(vault_path, note_content)
        update_shipped_slug(
            attempt_id=rec.id,
            shipped_slug=pair_slug,
            attempts_log_path=attempts_log_path,
        )
    elif rec.outcome == "dropped_agreement_reject":
        commit_outcome = "dropped_agreement_reject"
        synthesis_slug = None
        attempt_id = rec.id
    elif rec.outcome == "dropped_disagreement_exhausted":
        commit_outcome = "dropped_disagreement_exhausted"
        synthesis_slug = None
        attempt_id = rec.id
    else:
        # ``disagreement_pending`` is an in-flight state that should
        # never be the final ``run_dual_judge`` return value. Treat as
        # exhausted so the caller has a terminal outcome.
        commit_outcome = "dropped_disagreement_exhausted"
        synthesis_slug = None
        attempt_id = rec.id

    # 3. Pairs log line — every commit, ship or drop.
    _atomic_append_jsonl(
        pairs_path,
        {
            "ts": _now_iso(),
            "note_a": slug_a,
            "note_b": slug_b,
            "synthesis": synthesis_slug,
        },
    )

    return CommitResult(
        outcome=commit_outcome,
        synthesis_slug=synthesis_slug,
        attempt_id=attempt_id,
        fallback_reason=fallback_reason,
    )


def _write_vault_note(path: pathlib.Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp + rename) so a
    crashed write can't leave a half-file in the vault. Parent dir is
    created on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content = content + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
