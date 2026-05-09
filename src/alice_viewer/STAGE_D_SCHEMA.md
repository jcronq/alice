# Stage D review — storage schema

Source of truth for the dual-judge synthesis review pipeline. Two append-only
JSONL files in `~/alice-mind/inner/state/` plus an in-process join performed
by the viewer.

Design references:

- `cortex-memory/research/2026-05-08-stage-d-cap-redesign-quality-gated.md`
  (sections 9 + 10 are the SETTLED design)
- `cortex-memory/research/2026-05-09-stage-d-labeling-pipeline.md`

## Files

| Path                                            | Writer    | Append-only |
| ----------------------------------------------- | --------- | ----------- |
| `inner/state/stage-d-attempts.jsonl`            | thinking  | yes         |
| `inner/state/stage-d-labels.jsonl`              | speaking  | yes (newest entry per `attempt_id` wins on read) |

The viewer reads both, joins them on `attempt_id`, and never rewrites either
log. Missing files are treated as empty (no crash). Concurrent writers are
fine — both files are line-delimited JSON, append is atomic for typical line
sizes, and the join logic is order-independent.

## stage-d-attempts.jsonl

One line per Stage D synthesis attempt — produced by thinking when she runs
Stage D. **Every** attempt is logged regardless of outcome (per the firehose
directive in §10.2 of the cap-redesign doc). Auto-drop means "no vault write,"
not "invisible." This log is the calibration corpus.

```jsonc
{
  "id":             "att-2026-05-09T03-14-22-abc123",  // unique attempt id; uuid or ts-derived
  "pair": {
    "slug_a":       "research/2026-04-12-foo",
    "slug_b":       "people/jason"
  },
  "synthesis_text": "…full synthesis body, markdown, multi-line OK…",
  "draft_attempt_n": 1,                                // 1, 2, or 3 (per §2.3 reassess loop)

  "qwen_verdict": {
    "tier":     "T1",                                  // T1 | T2 | T3 | T4
    "novel":    "yes",                                 // yes | no
    "reason":   "one-sentence justification",
    "decision": "ship"                                 // ship | reject
  },
  "haiku_verdict": {
    "tier":     "T2",
    "novel":    "yes",
    "reason":   "one-sentence justification",
    "decision": "ship"
  },

  "outcome":        "shipped",                         // see Outcomes below
  "retry_history":  [],                                // list[str] — prior synthesis_text values from
                                                       // earlier draft_attempt_n on the same pair
  "created_at":     "2026-05-09T03:14:22-04:00",       // ISO8601 with offset
  "shipped_slug":   "research/2026-05-09-foo-x-jason-creativity",  // null unless outcome=="shipped"
  "jason_label":    null                               // legacy/optional; the canonical label is in
                                                       // stage-d-labels.jsonl. Viewer ignores this
                                                       // field on read and joins the sidecar instead.
}
```

### Outcomes

| Value                              | Meaning                                                                    |
| ---------------------------------- | -------------------------------------------------------------------------- |
| `shipped`                          | Both judges said `ship`. Synthesis written to vault at `shipped_slug`.     |
| `dropped_agreement_reject`         | Both judges said `reject`. No vault write.                                 |
| `dropped_disagreement_exhausted`   | Disagreement persisted through 3 attempts. No vault write. Pair logged.   |
| `disagreement_pending`             | Disagreement on the current attempt; reassess loop still in progress.     |

## stage-d-labels.jsonl

Sidecar — Jason's labels live here so the attempt log stays append-only and
unmutated. One line per label event. **Newest entry wins** when the viewer
joins on read (operator can re-label by appending a new line).

```jsonc
{
  "attempt_id": "att-2026-05-09T03-14-22-abc123",
  "label":      "T1",                          // T1 | T2 | T3 | T4 | ship | reject | unlabeled
  "label_axes": {                              // optional, free-form multi-axis future hook
    "novelty":  "high",
    "rigor":    "med"
  },
  "labeled_at": "2026-05-09T08:42:11-04:00"   // ISO8601 with offset
}
```

`label_axes` is optional. The viewer's one-keystroke flow only sets `label`;
multi-axis is a Phase 2 extension that doesn't require a schema migration.

`label == "unlabeled"` is the explicit "clear my prior label" signal. The
join logic still picks the newest line, so writing `unlabeled` is how Jason
takes back a misclick.

## Join semantics

Pseudocode for the viewer's read path:

```python
attempts = read_jsonl("stage-d-attempts.jsonl")           # list[dict]
labels   = read_jsonl("stage-d-labels.jsonl")             # list[dict]

# Build a {attempt_id -> latest label record} dict by iterating in file order.
# JSONL is append-only, so file order == time order. Last write wins.
latest_label: dict[str, dict] = {}
for rec in labels:
    latest_label[rec["attempt_id"]] = rec

for att in attempts:
    att["label_record"] = latest_label.get(att["id"])     # None if unlabeled
```

The viewer never opens either file in write mode for `stage-d-attempts.jsonl`.
For `stage-d-labels.jsonl` it only ever appends a new line — never rewrites,
never sorts, never compacts.

## Fixtures

For development, `python -m alice_viewer.stage_d_store --regen-fixtures` (or
running the module directly) writes 4 sample attempts to
`stage-d-attempts.jsonl` (one each: shipped, dropped_agreement_reject,
disagreement_pending, dropped_disagreement_exhausted-with-retry-history).

To clear all fixture state and start fresh:

```bash
rm -f ~/alice-mind/inner/state/stage-d-attempts.jsonl \
      ~/alice-mind/inner/state/stage-d-labels.jsonl
python -m alice_viewer.stage_d_store --regen-fixtures
```

The fixture writer **refuses to run** if the attempts file already contains
non-fixture entries (any `id` not starting with `att-fixture-`). This protects
real production data from accidental overwrites once thinking starts writing.
