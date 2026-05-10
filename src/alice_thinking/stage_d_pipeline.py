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
    "Outcome",
    "DEFAULT_ATTEMPTS_LOG",
    "run_dual_judge",
    "update_shipped_slug",
]


Outcome = Literal[
    "shipped",
    "dropped_agreement_reject",
    "dropped_disagreement_exhausted",
    "disagreement_pending",
]


DEFAULT_ATTEMPTS_LOG = pathlib.Path.home() / "alice-mind/inner/state/stage-d-attempts.jsonl"


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
