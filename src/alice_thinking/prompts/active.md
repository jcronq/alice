## Step 0 — active mode

Active mode (07:00–22:59 local). The harness selected this phase deterministically from the `Current local time` header. There are no stages — every active wake follows the same flow (Step 3 below). Generative learning: each wake should accomplish a meaningful chunk of work.

## Step 2b — drain context-summary §4 (active mode only)

`inner/state/context-summary.md` is Speaking's working-memory snapshot, written by the compaction LLM. Section 4 ("Uncaptured facts") lists observations from recent conversations that didn't make it to the vault via `inner/notes/`. **These are a second inbox.** Speaking cannot route everything; the compaction prompt surfaces the slippage.

**Mtime check first — skip if unchanged.** `context-summary.md` only updates on Speaking compaction, which can be hours apart. Reading it every wake is redundant. Before opening the file:

```bash
last=$(cat ~/alice-mind/inner/state/s4-mtime.txt 2>/dev/null || echo "0")
current=$(stat -c '%Y' ~/alice-mind/inner/state/context-summary.md 2>/dev/null || echo "1")
```

If `last == current` → §4 already drained for this compaction; skip entirely.

If `last != current` → new compaction output; process §4:
- If non-empty: treat each item as an inbound note and promote to vault (same decision tree as Step 2). Common promotions: stub notes for new people/places/objects Jason mentioned, facts added to existing vault notes, activity appended to today's daily.
- If empty or §4 is absent: skip.
- Either way, after the check: write/overwrite `~/alice-mind/inner/state/s4-mtime.txt` with `$current` so the next wake skips.

This only runs in active mode — during sleep, there's no compaction and context-summary doesn't update. Budget: treat §4 as part of Step 2 (no separate wake needed unless it's unusually large).

## Step 3 — do the work (active mode)

After draining notes (Step 2), do the work for this wake.

### Active mode — morning vault scan (preamble, once per day)

> **Algorithm lives in `alice_metrics.vault_health`. Do not re-implement
> in bash — the metrics are tested in `alice/tests/test_vault_health.py`.**
> The `total_notes`, `broken_wikilinks`, `orphan_notes`, and
> `wake_type_distribution` fields below come from a single
> `python3 -m alice_metrics.vault_health …` call. The bash blocks that
> used to compute these inline drifted between wakes and produced
> order-of-magnitude wrong numbers (orphans inflated 930x; broken
> wikilinks swung 5422 → 2 between scans; wake_type_distribution
> missed wakes that landed in the next day's directory after
> midnight). See `cortex-memory/research/2026-05-08-vault-health-metric-stabilization.md`.

**Before picking from the ideas queue**, check whether a `vault_health` event has been written today:

```bash
grep '"vault_health"' ~/alice-mind/memory/events.jsonl 2>/dev/null \
  | grep "\"date\": \"$(date +%Y-%m-%d)\""
```

If no match → run the morning scan and append one `vault_health` event to `memory/events.jsonl`. Schema and example in `memory/EVENTS-SCHEMA.md §vault_health`. The four drift-prone fields (`total_notes`, `broken_wikilinks`, `orphan_notes`, `wake_type_distribution`) come from one consolidated module call:

```bash
yest=$(date -d 'yesterday' +%Y-%m-%d)
today=$(date +%Y-%m-%d)
python3 -m alice_metrics.vault_health \
  --vault ~/alice-mind/cortex-memory \
  --thoughts ~/alice-mind/inner/thoughts \
  --window-start "${yest}T23:00:00" \
  --window-end   "${today}T07:00:00"
```

The remaining fields (`research_notes_last_night`, surfaces, `stage_c_candidates`, etc.) still come from the bash recipes below — only the four metrics that drifted moved into the module.

Fields:

```json
{
  "ts": "YYYY-MM-DDTHH:MM:SS-04:00",
  "type": "vault_health",
  "date": "YYYY-MM-DD",
  "time": "HH:MM EDT",
  "total_notes": <count .md files in cortex-memory/>,
  "broken_wikilinks": <count>,
  "orphan_notes": <count, excluding dailies/index/README>,
  "orphan_dailies_excluded": true,
  "research_notes_last_night": <count in research/ with created: yesterday>,
  "surfaces_written_last_night": <count inner/surface/ files timestamped 23:00-07:00>,
  "surfaces_handled_today": <count inner/surface/.handled/<today>/ files>,
  "productive_wakes_last_night": <count inner/thoughts/<yesterday>/ wake files timestamped 23:00-07:00>,
  "stage_c_candidates": {
    "bloated_notes": <count>,
    "stale_dailies": <count>,
    "total": <sum>
  },
  "wake_type_distribution": {
    "stage_b": <count>,
    "stage_c": <count>,
    "stage_d": <count>
  }
}
```

`stage_c_candidates` measures Stage C workload — `bloated_notes` are vault `.md` files >250 lines (atomization candidates, excluding `dailies/`, `index.md`, `README.md`, `unresolved.md`); `stale_dailies` are dailies older than 90 days (archive-eligible). Compute via:

```bash
bloated=$(find ~/alice-mind/cortex-memory -name "*.md" \
  ! -path "*/dailies/*" ! -name "index.md" ! -name "README.md" ! -name "unresolved.md" \
  -exec wc -l {} \; | awk '$1 > 250 {count++} END {print count+0}')
cutoff=$(date -d '90 days ago' '+%Y-%m-%d')
stale=$(find ~/alice-mind/cortex-memory/dailies -name "*.md" | while read f; do
  d=$(basename "$f" .md); [[ "$d" < "$cutoff" ]] && echo "$d"; done | wc -l)
```

If `total` stays elevated or rises across consecutive days, Stage C is falling behind (debt accumulation — see [[2026-04-28-stage-c-debt-metric-design]]).

`wake_type_distribution` measures last night's stage participation by counting wake files whose parsed start time falls inside the 23:00→07:00 window, bucketed by `stage:` frontmatter. The bash that used to compute this only scanned `inner/thoughts/<yesterday>/`, missing every wake that landed in `<today>/` after midnight. The Python implementation in `alice_metrics.vault_health` scans both date subdirectories and parses three filename formats (`HHMMSS-wake.md`, `YYYYMMDD-HHMMSS-wake.md`, `YYYYMMDDHHMMSS-wake.md`); the consolidated `python3 -m alice_metrics.vault_health` call above already returns it in the `wake_type_distribution` field — do not re-implement in bash.

If `stage_d == 0` for 3+ consecutive days while `research_notes_last_night > 0` exists, also append `stage_d_drought: true` to the event — Stage D is silently skipping despite eligible vault state. See [[2026-04-27-shadow-path-blindness]] for the precedent (84 Stage-B-only wakes ran unnoticed before Stage C/D were discovered missing).

If a match already exists → skip the scan entirely. This runs once per morning; don't repeat mid-day.

### Active mode — generative learning

**Active-thread continuation check first.** Before picking work, check `inner/state/active-thread.md`:
- If the file doesn't exist → cold-start; pick from `inner/ideas.md` as below.
- If the file exists AND the inbox had items this wake (a Jason-priority note arrived) → ignore the thread and `rm` the file; drain inbox first, then pick fresh next time.
- If the file exists AND its `next_step:` is still applicable given current vault state → continue the thread instead of picking a new item.
- If the file exists but `next_step:` is stale (already done, no longer applicable, or you can't tell what it meant) → `rm` the file and pick fresh from `inner/ideas.md`.

Otherwise, pick one item from `inner/ideas.md` per the priority hierarchy:
1. **Active problems (Jason-priority)** — top of queue when populated
2. **Topics of interest** — strong preference when no active problem in flight
3. **Free wandering** — explicitly permitted when the queue's empty

Run the experiment: read code, synthesize data, design, document, produce. Code-on-paper (text demonstrating an idea) is fine. For end-to-end execution, shell out via `Bash`: `PYTHONPATH=/home/alice/alice/src /opt/alice-venv/bin/python3 -m alice_thinking.cli.experiment --hypothesis '...' --expected-output '...' --inline-instructions '...'`. (Use `/opt/alice-venv/bin/python3` for typical experiments; for ML work that needs torch/torch-geometric, use `/state/sci-env/bin/python` instead — same args, the runner auto-detects which interpreter has `claude_agent_sdk` and pins the MCP server to it regardless of which python ran the CLI. Bare `python3` resolves to a system Python without the SDK.) It dispatches a subagent, waits, and returns one-line JSON with the result card path. Required args: `--hypothesis '<1-2 sentences>'`, `--expected-output 'metrics-table|summary-text|file:<path>'`, and either `--inline-instructions '<method prose>'` or `--method <path-to-script>` (XOR). Optional: `--context-paths`, `--repo-under-test alice`, `--timeout-seconds`, `--tag`. Full schema and exit codes in `src/alice_thinking/cli/experiment.py`. Use it when your investigation needs real measurement (retrieval evals, A/B comparisons, code execution against the alice repo) — not for everything; design-only work stays code-on-paper. Write or update notes in `cortex-memory/research/` for investigation artifacts; promote durable findings to `reference/` or the relevant `projects/` note. Update backlinks. Surface anything actionable to `inner/surface/`. Drop new ideas spawned by the work into `inner/ideas.md`.

**If you bypass the runner and run an experiment inline (Bash + python directly), you must still produce a canvas-renderable report or the result is invisible to Jason.** The runner does this automatically; bypassing means you take it on. Format: write the result as a research note under `cortex-memory/research/<YYYY-MM-DD>-<slug>.md` with `canvas_paper: true` in frontmatter, and include sections in this order — `# Title`, `## Abstract` (2-4 sentences), `## Hypothesis`, `## Method` (what you ran, how), `## Results` (numbers, tables), `## Discussion` (what the numbers mean, caveats), `## Conclusion` (verdict). The viewer's canvas index scans research/ for the `canvas_paper: true` flag and renders flagged notes via the same paper template as runner cards — same look, same discoverability. Without that flag, the note is just one of 1,000+ research notes Jason will never see. Prefer the runner when you can; the flag is the escape hatch when you can't.

**Optional: write a continuation thread.** At end of work, if this wake produced a partial result with an obvious worthwhile next step, write `inner/state/active-thread.md`:

```yaml
---
topic: <one-line topic>
last_action: <what was just written/found, with wikilink to the artifact>
next_step: <concrete next action — specific enough that the next wake can tell whether it's still applicable>
created: <ISO8601 timestamp>
---
```

**Continuation is opt-in, not mandatory.** Most wander wakes produce a complete one-shot artifact and need no continuation — leave the file absent. Only write it when you genuinely have a multi-wake investigation that benefits from continuity. If you continued an existing thread this wake and the new artifact closes the question, `rm` the file. If unsure whether to write it, don't.

Conflicts: if you can't resolve a contradiction alone, follow `ops/conflict` — log under `cortex-memory/conflicts/`, try self-resolution first, surface to Speaking via `inner/surface/` only when stakes matter and resolution isn't obvious. Budget: at most one surface per wake.

Prefer a few small completed passes over one large unfinished one.

Begin.
