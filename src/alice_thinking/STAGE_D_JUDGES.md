# Stage D dual-judge quality gate — call shapes

Send-side counterpart to the alice-viewer Stage D review tab (PR #29).
Two modules; the wake prompt drives them.

Design source of truth:
- `cortex-memory/research/2026-05-08-stage-d-cap-redesign-quality-gated.md` (umbrella)
- `cortex-memory/research/2026-05-08-stage-d-dual-judge-protocol.md` (§2.3 reassess loop)
- `cortex-memory/research/2026-05-09-stage-d-judge-prompts.md` (per-model prompts)
- `cortex-memory/research/2026-05-09-stage-d-labeling-pipeline.md` (firehose log)
- Receive-side schema: `src/viewer/STAGE_D_SCHEMA.md`

## Modules

| Module | Purpose |
| --- | --- |
| `alice_thinking.stage_d_judges` | One-shot judge calls (Qwen, Haiku) with bias-compensated prompts. |
| `alice_thinking.stage_d_pipeline` | Agreement + reassess protocol, JSONL firehose writer, **deterministic commit** entry point. |
| `alice_thinking.stage_d_invariant` | Post-wake audit: scan vault for `source: stage-d` notes that lack an attempts.jsonl ship line or judge-failures.jsonl fallback line. Belt + suspenders against end-runs around the gate. |

## Entry point: `commit_stage_d_synthesis` (preferred)

This is the **single call** that owns judge dispatch, the firehose log,
the vault note write, the shipped-slug update, the judge-failure
fallback, and the pairs-log append. The wake prompt should call this
once per pair — there is no way to ship a Stage D synthesis except
through it.

```python
from alice_thinking.stage_d_pipeline import commit_stage_d_synthesis

result = commit_stage_d_synthesis(
    slug_a="research/foo",
    slug_b="people/jason",
    source_a_text=open("cortex-memory/research/foo.md").read(),
    source_b_text=open("cortex-memory/people/jason.md").read(),
    draft_synthesis="<3-6 sentence body>",
    output_slug="2026-05-11-foo-x-jason-creativity",
    note_content="<full markdown including frontmatter with source: stage-d, note_a, note_b>",
    prior_pair_synthesis=None,
)
# result.outcome ∈ {"shipped", "dropped_agreement_reject",
#                   "dropped_disagreement_exhausted", "fallback"}
# result.synthesis_slug      — "research/<output_slug>" on ship/fallback, None on drop
# result.attempt_id          — from run_dual_judge, None when fallback
# result.fallback_reason     — short error string, populated only when fallback
```

Side effects, in order:

1. Per-attempt JSONL lines appended to `stage-d-attempts.jsonl` (firehose).
2. If judges raise: one JSONL line appended to `stage-d-judge-failures.jsonl`; **no** attempts log entry.
3. Vault note written atomically (temp + rename) to `<vault_root>/research/<output_slug>.md` on `shipped` or `fallback`. NOT written on drop outcomes.
4. On `shipped`: `update_shipped_slug` rewrites the most recent attempt line with the slug.
5. One JSONL line appended to today's `stage-d-pairs-YYYY-MM-DD.jsonl` with `{ts, note_a, note_b, synthesis: <slug-or-null>}`. Appended on every commit including drops.

The caller picks the `output_slug` and is responsible for the vault
note's full markdown content (including frontmatter). The pipeline does
not validate frontmatter — that's the invariant check's job (below).

## Invariant check: `find_unaudited_stage_d_notes`

```python
from alice_thinking.stage_d_invariant import find_unaudited_stage_d_notes
import datetime

bad = find_unaudited_stage_d_notes(date=datetime.date.today())
# bad is a list[dict]; empty when every source: stage-d note created today
# has either a matching attempts.jsonl ship line or a matching
# judge-failures.jsonl fallback line.
```

Stage D wakes run this at close-clean. If `bad` is non-empty, the wake
files a surface under `inner/surface/` listing each unaudited note.
This catches any future regression (or hand-edit) that bypasses
`commit_stage_d_synthesis`.

## `judge_qwen` / `judge_haiku`

```python
from alice_thinking.stage_d_judges import judge_qwen, judge_haiku, Verdict, JudgeOutputError

verdict: Verdict = judge_qwen(
    synthesis="<3-6 sentence synthesis text>",
    source_a_text="<full body of note A>",
    source_b_text="<full body of note B>",
    prior_pair_synthesis=None,  # or "<text>" if this pair shipped before
)
# verdict = {"tier": "T1", "novel": True, "reason": "...", "decision": "ship"}
```

- **Qwen** dispatches via google-adk's `LiteLlm` adapter to the LAN endpoint
  at `http://10.20.30.177:8033/v1`, model `openai/Qwen3-Coder-Next`.
  No auth — endpoint is unauthenticated.
- **Haiku** dispatches via the `anthropic` SDK using
  `claude-haiku-4-5-20251001`. Reads `ANTHROPIC_API_KEY` from environment.
- Both raise `JudgeOutputError` on malformed model output (empty,
  invalid JSON, bad enum, missing field, missing API key). Caller decides
  retry semantics.
- Bias compensation lives in the prompt text, not in code:
  - Qwen prompt damps verbosity, frames reject as valid.
  - Haiku prompt invites skepticism, frames thoroughness as the value-add.

## `run_dual_judge`

```python
from alice_thinking.stage_d_pipeline import run_dual_judge, AttemptRecord

rec: AttemptRecord = run_dual_judge(
    slug_a="research/foo",
    slug_b="people/jason",
    source_a_text=open("cortex-memory/research/foo.md").read(),
    source_b_text=open("cortex-memory/people/jason.md").read(),
    draft_synthesis_fn=my_drafter,           # see contract below
    prior_pair_synthesis=None,
    # attempts_log_path defaults to ~/alice-mind/inner/state/stage-d-attempts.jsonl
    # max_attempts defaults to 3
)
# rec.outcome ∈ {"shipped", "dropped_agreement_reject", "dropped_disagreement_exhausted"}
```

`draft_synthesis_fn` is called with `prior=None` on the first attempt and
with the prior draft text on each disagreement reassess. It must return a
non-empty string.

The pipeline appends **one JSONL line per judge attempt** (firehose). On
ship, the caller writes the vault note and then calls:

```python
from alice_thinking.stage_d_pipeline import update_shipped_slug
update_shipped_slug(attempt_id=rec.id, shipped_slug="research/2026-05-09-foo-x-jason")
```

This rewrites the most recent JSONL line for `rec.id` in place with the
slug filled in. Idempotent; no-op if the id isn't found.

## JSONL append contract

- File: `~/alice-mind/inner/state/stage-d-attempts.jsonl` (override via
  `attempts_log_path`).
- Mode: append; one line per call to `run_dual_judge`'s inner judge step.
- Encoding: UTF-8, no BOM, line-delimited JSON, no trailing comma.
- Atomic: per-line append within typical sizes (< pipe buffer). The
  writer opens, writes, flushes, closes per line.
- Schema: matches `src/viewer/STAGE_D_SCHEMA.md` field-for-field —
  `id`, `pair`, `synthesis_text`, `draft_attempt_n`, `qwen_verdict`,
  `haiku_verdict`, `outcome`, `retry_history`, `created_at`,
  `shipped_slug`. `Verdict.novel` is emitted as a bool (the schema doc
  shows `"yes"`/`"no"` strings — both are accepted on the read side; the
  writer always emits bool).
- Append-only invariant for the receiver. The only mutation is
  `update_shipped_slug`, which rewrites the matching line in place via
  temp+rename — by design the viewer is order-independent on read.

## Environment

- `ANTHROPIC_API_KEY` — required for Haiku judge. Already set in the
  worker container env.
- LAN Qwen endpoint at `http://10.20.30.177:8033/v1` — no auth, no
  proxy. Reachable from worker containers; not always reachable from
  test harnesses (tests mock both call sites).

## Failure mode

If the python pipeline raises (module import error, env var missing,
LAN endpoint unreachable, OOM, etc.), `commit_stage_d_synthesis`
catches the exception, appends one JSONL line to
`~/alice-mind/inner/state/stage-d-judge-failures.jsonl` (with `ts`,
`slug_a`, `slug_b`, `reason`), and falls back to writing the vault
note from the unchanged draft. The returned `CommitResult.outcome` is
`"fallback"`; `result.fallback_reason` carries the short error.

Review the failures log when investigating "Stage D went silent" —
it's the breadcrumb trail.

The wake prompt does NOT have its own fallback code path. The pipeline
owns the fallback so callers can't accidentally diverge from the
documented contract.

## Test-time mocking

Both modules expose injection seams:

- `stage_d_judges._call_qwen(prompt: str) -> str` and
  `stage_d_judges._call_haiku(prompt: str) -> str` — monkeypatch these
  to return canned strings; the public `judge_*` callers parse them
  through the shared verdict-validation logic.
- `run_dual_judge(..., judge_qwen_fn=..., judge_haiku_fn=...)` — pass
  callables that return pre-built verdict dicts to skip prompting
  entirely.
- `commit_stage_d_synthesis(..., judge_qwen_fn=..., judge_haiku_fn=...)` —
  same seam, forwarded through to the underlying `run_dual_judge`. To
  exercise the fallback path, pass a callable that raises.

Tests live in `tests/test_stage_d_judges.py`, `tests/test_stage_d_pipeline.py`,
and `tests/test_stage_d_commit.py`. None of them make live LLM calls.
