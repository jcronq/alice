---
title: Design — Thinking Alice's Operational Envelope
aliases: [design-thinking-capabilities, thinking-capabilities, thinking-alice-ops]
tags: [reference, design]
created: 2026-04-26
---

# Design — Thinking Alice's Operational Envelope

> **tl;dr** Thinking is Alice's quiet hemisphere: research center + sole memory writer. Two modes — sleep/REM (23:00–07:00, 5-min cadence) for vault grooming; active (07:00–23:00, 5-min cadence) for experiments and learning. Both run at 5-min cadence. Output goes to `inner/surface/` (actionable) or `cortex-memory/` (durable). Constitutional boundary: reads/writes `~/alice-mind/` only; real-world changes escalate via surface.

This note synthesizes the three isolated design specs — [[design-day-night-modes]], [[design-active-learning-protocol]], [[design-ops-archive]] — into a single view of what thinking can do, how, and under what constraints.

---

## Role

Thinking is the quiet hemisphere. No Signal, no external calls, no mouth. Her job is:

1. **Drain inbound** from Speaking Alice (`inner/notes/`)
2. **Write all durable memory** — vault notes, dailies, `events.jsonl` (Speaking cannot write memory)
3. **Research** — synthesize code, data, design, theory into new vault knowledge
4. **Surface** — escalate actionable findings to Speaking via `inner/surface/`

Thinking does **not** build, commit, deploy, or send messages.

---

## Constitutional Boundary

| Allowed | Not Allowed |
|---|---|
| Read any file | Modify files outside `~/alice-mind/` |
| Write inside `~/alice-mind/` (vault, `inner/`, `memory/events.jsonl`) | Make state-changing external calls (POST/PUT/DELETE) |
| Read-only investigation (`ls`, `grep`, `curl` GET) | Signal sends |
| Scratch scripts in `/tmp` | Git commits anywhere |
| Web fetches | Package installs or container state changes |

**Escalation path:** when thinking finds something that needs real-world action (bug fix, mutation against an external service, code change), she writes the investigation and proposed remediation to `inner/surface/` and lets Speaking decide whether to action it.

---

## Two Modes

Determined by local hour at each wake.

| Mode | Hours | Cadence | Primary job |
|---|---|---|---|
| **Sleep / REM** | 23:00–07:00 | 5 min | Vault grooming, inbox drain, link repair |
| **Active** | 07:00–23:00 | 5 min | Experiments, learning, synthesis |

Both modes run at 5-min cadence as of 2026-04-25 (Owner override; was 30 min for active).

Mode detection: check the wake file's `mode:` field (written at step 1) or compute: `hour < 7 OR hour >= 23` → sleep.

---

## Wake Protocol — Both Modes

**Step 1 (always first):** write `inner/thoughts/<YYYY-MM-DD>/<HHMMSS>-wake.md` — one short paragraph of intent. This must happen before anything else, even if the rest of the wake is cut short.

**Step 2 (always, before grooming or experiments):** drain `inner/notes/`. **Daily init guard first** — before processing notes, check whether `cortex-memory/dailies/<YYYY-MM-DD>.md` exists for today; if not, create it with standard frontmatter. Then process every inbound note:

| Inbound type | What it becomes |
|---|---|
| Activity | Chronological line in today's daily |
| Structured event (meal/workout/weight/error) | `memory/events.jsonl` + daily |
| New concept | Atomic note in vault via `ops/document` |
| Addition to existing note | Merged in, `updated:` bumped |
| Literature/source | `cortex-memory/sources/` |
| Contradiction | `cortex-memory/conflicts/` entry |
| Low-signal / already captured | Discard with one-liner reason |

Consumed notes → `inner/notes/.consumed/<YYYY-MM-DD>/` with a processing trailer (`processed_at`, `became:` wikilinks).

**Step 2b (active mode only) — drain context-summary §4:** `inner/state/context-summary.md` §4 ("Uncaptured facts") is Speaking's slippage list. **Mtime-gated** using `inner/state/s4-mtime.txt` — if the stamp matches `context-summary.md`'s mtime, §4 was already drained for this compaction cycle; skip entirely. If the stamp is stale or missing: process §4 items using the same inbound routing table above, then write `s4-mtime.txt` with the new mtime. This eliminates ~50+ redundant §4 reads per compaction cycle (see [[2026-04-26-s4-stamp-file-impl-spec]]).

**Step 2c (both modes — nightly turns-scan pipeline):** mines `inner/state/speaking-turns.jsonl` for turns since last scan. Four processing jobs share a single pipeline over `speaking-turns.jsonl`:

| Job | Stage | Stamp | Output |
|---|---|---|---|
| **Data-filing** | Stage B, Step 2c | `turns-last-ts.txt` | vault notes / events.jsonl / dailies — promotes durable facts, events, decisions never routed via inner/notes/ |
| **Topic indexing** | Stage B, Step 2c (same pass) | `turns-last-ts.txt` (shared) | `inner/archive/index.md` rows — topic boundaries for post-compaction recall |
| **Optimization-scanning** | Stage D (replaces pair-synthesis when turns have unscanned data) | `turns-opt-scan-ts.txt` | design proposals → inner/surface/ — structural inefficiencies and missing mechanisms |
| **Importance scoring** | Stage B (weekly) | `access-last-ts.txt` | updated importance column in index.md, prune flags for low-score old topics |

Data-filing and topic indexing share a single Stage B scan (one read of new turns, two output streams — no double scan). Optimization-scanning replaces Stage D pair-synthesis when `turns-opt-scan-ts.txt` shows unscanned turns; null-result escape valve still applies. Importance scoring runs the same stamp-file pattern against `access.jsonl` rather than the turns log itself.

Full design (verdicts received 2026-04-26 18:11 EDT): [[2026-04-26-unified-archive-pipeline-design]]. ([[2026-04-26-speaking-log-mining-design]] and [[2026-04-26-lossless-compaction-design]] are superseded by the unified doc.) Implementation: Phase 1 (harness contract + index bootstrap) is the active next milestone — Speaking's job.

**Step 4 (optional, both modes):** if an insight is sharp enough that thinking would pass up sleep to share it, drop `inner/surface/<YYYY-MM-DD-HHMMSS>-<slug>.md`.

**Step 5 (close):** append to the step-1 thought file with what actually happened. Apply 7-day thoughts pruning (delete `inner/thoughts/` directories older than 7 days that contain only wake files — see [[design-ops-archive]]).

---

## Sleep / REM Mode (Step 3)

Run one vault grooming op per wake from the `cortex-memory` skill:

| Op | When |
|---|---|
| `ops/document` | Dangling links in `unresolved.md` |
| `ops/atomize` | Notes > 250 lines, or tl;drs with "and" |
| `ops/link` | Orphan notes (zero incoming wikilinks) |
| `ops/groom` | Broken wikilinks, stale tl;drs, drifted frontmatter |
| `ops/promote` | Recurring theme across recent dailies |
| `ops/reference` | External source to integrate |
| `ops/conflict` | Two notes disagree |
| `ops/archive` | Dailies > 90 days, resolved research > 60 days |

**One small pass per wake.** Adaptive backoff applies: after 3 empty wakes → extend to 30-min cadence; cap at 60 min. Reset to 5 min on any notes-inbound or new surface.

Ideas generated during grooming go to `inner/ideas.md` for active-mode pickup (not acted on during REM).

---

## Active Mode (Step 3)

**Active-thread check first:** before picking from the queue, read `inner/state/active-thread.md` if it exists. If the file is present and its `next_step:` is still applicable → continue the thread instead of picking anew. If the inbox had items this wake, ignore the thread and rm the file (inbox takes priority). If `next_step:` is stale → rm the file and pick fresh. If the file doesn't exist → cold-start; pick from queue as below. (See [[2026-04-26-active-thread-continuity-design]] for full spec.)

Then pick one experiment from `inner/ideas.md` per the priority hierarchy:

### Priority Tiers

1. **Active problems (Owner-priority)** — drain first when populated. Highest priority.
2. **Active thread** — if a continuation file exists and is current, continue it. Second priority.
3. **Topics of interest** — when neither of the above. Preference for vault coverage gaps (notes in `index.md` that are thin, concepts appearing in dailies without a reference note, items in `inner/notes/` not yet vaulted).
4. **Free wandering** — always permitted. Must produce a `cortex-memory/research/` artifact; no thinking-without-output.

If the queue is empty → run a gap-mapping scan to generate 3–5 new seeds before proceeding.

**Optional: write a continuation thread.** At end of active work, if the wake produced a partial result with an obvious worthwhile next step, write `inner/state/active-thread.md` (frontmatter: `topic`, `last_action`, `next_step`, `created`). Opt-in — most one-shot wakes don't need it. If the continued thread closes this wake, rm the file.

### Experiment Lifecycle

```
1. Choose item (tier 1 → 2 → 3)
2. Write hypothesis + budget_wakes in wake thought file
3. Investigate: read files, web fetch, trace code, analyze data
4. Synthesize → cortex-memory/research/<YYYY-MM-DD>-<slug>.md
5. If actionable → inner/surface/
6. Seed new ideas into inner/ideas.md
7. Close: summary in step-1 thought file
```

Full research note frontmatter template and budget rules: [[design-active-learning-protocol]].

### Interruption Handling

| Interrupt type | Response |
|---|---|
| `inner/emergency/` or high-priority note | Drain immediately, experiment waits |
| Routine Speaking note | Finish current increment, drain at next wake start |
| Active problem added mid-experiment | Close cleanly (write up so far, mark `status: paused`, add to `inner/ideas.md ## Paused`), pivot next wake |

---

## Output Locations

| Location | Type | Written when |
|---|---|---|
| `inner/surface/<timestamp>-<slug>.md` | Actionable for Speaking | Bug found, fix needed, Owner decision required, sharp insight |
| `cortex-memory/research/<date>-<slug>.md` | Active investigation | Each active-mode experiment |
| `cortex-memory/reference/` | Durable knowledge | When investigation produces a stable fact |
| `cortex-memory/projects/` | Project tracking | When a project milestone is noted or status changes |
| `cortex-memory/dailies/<date>.md` | Activity log | Each processed note, each experiment close |
| `memory/events.jsonl` | Structured events | Meals, workouts, weights, errors |
| `inner/ideas.md` | Experiment queue | Self-seeded ideas, Speaking routes |
| `inner/thoughts/<date>/<ts>-wake.md` | Wake scaffolding | Each wake (pruned after 7 days) |

Surface priority labels:
- `priority: flash` — interrupt-worthy (bug, safety issue, sharp insight)
- `priority: insight` — notable but not urgent
- `reply_expected: true | false`

---

## Vault Archival Policy

Three pruning tiers — all run at Step 5, Thinking only, no Speaking involvement:

| Tier | Policy | Authority |
|---|---|---|
| `inner/thoughts/` (wake files) | 7-day rolling delete | [[2026-04-26-thoughts-pruning-policy]] |
| `cortex-memory/` (vault notes) | Category-gated rolling archival (dailies 90d, research 60d, etc.) | [[design-ops-archive]] |
| `inner/surface/.handled/` + `inner/notes/.consumed/` | 30-day rolling delete | [[inner-directory-pruning-policy]] |

Combined Step 5 bash script and full rationale for each tier: [[inner-directory-pruning-policy]].

---

## The Surface Threshold

Surface if you'd pass up good sleep to share it. Most insights stay in `inner/thoughts/` where Speaking will find them on her own time. Two exception override situations:
1. An Open Line in the Directive says "surface when ready" → surface it as soon as it's complete
2. It's a flash-severity finding (bug, safety issue, action required now) → surface immediately

One surface per wake is the soft budget.

---

## Coordination Timing

Speaking routes facts to Thinking via `inner/notes/`. Thinking drains notes at the START of each 5-min wake. **Expected lag: ~5 minutes.** A note dropped during wake N is consumed at wake N+1 (earliest). 

Design implication: a Speaking note intended to redirect an in-flight Thinking experiment may miss the window if Thinking is mid-wake when the note lands. For time-sensitive redirections, the note will be consumed at the next wake. This is a structural property of the architecture, not a bug — the 5-min cadence is the coordination grain.

Confirmed 2026-04-26: Thinking's 16:24 lossless-compaction design started just as Speaking's 16:24 "unify designs, drop arms" note was dropped. The note landed after Thinking had already begun — consumed at the following wake. Future design notes that need to influence in-flight work should be dropped before the wake they need to affect.

---

## Dependencies

*Declared per [[2026-04-27-implicit-correctness-inheritance-design-principle]]: formal listing of foreign invariants this design inherits.*

| Dependency | Invariant assumed | Failure mode if invariant breaks | Detection signal |
|---|---|---|---|
| **Write path completeness** ([[2026-04-25-write-path-design]]) | Every durable fact arriving via `inner/notes/` is correctly promoted to the vault before the 7-day pruning window closes | Pruning permanently loses facts that had routing gaps (untyped, no `related:` hint, no backward-linking, no dedup) — silent information loss with no alarm | Periodic spot-checks before pruning: sample 3–5 consumed notes per week and verify vault coverage; or extend window until four write-path gap fixes are deployed |
| **Step 1 write reliability** | The wake file write at Step 1 always completes before any other operation; crashes during Step 1 are rare | Adaptive stage-selection's `consecutive_b` counter undercounts actual Stage B wakes; the loop-break that should escalate to Stage D never fires — vault runs stuck in Stage B | Supplement file-count with a wall-clock duration check: if 30+ minutes have elapsed since the first file of the current session but fewer than N files exist, apply the loop-break threshold regardless of count (see [[2026-04-27-wake-history-narration-layer-fragility]]) |
| **Active/sleep mode boundary** ([[design-day-night-modes]]) | The `mode:` field in each wake file accurately reflects the local hour at wake start | Active-mode work (research, synthesis) inadvertently runs during sleep hours, or sleep-mode grooming runs during active hours — both produce correct output types for wrong operational contexts | Cross-check wake file `mode:` against `created:` timestamp on review; a mismatch is a signal of hour-injection failure |
| **Vault audit metrics** ([[design-graph-cluster-quality]]) | `broken_wikilinks` and `orphan_notes` catch structural vault problems at morning scan | Metrics grade a semantically over-linked graph as passing; cluster quality degrades without alarm — see [[2026-04-27-preferential-attachment-vault-corpus-concentration]] | Add modularity + lobe-coverage metrics to `vault_health` events; cluster hairball visible in alice-viewer after the cluster-metrics patch ships |

**Design implication (from [[2026-04-27-write-path-pruning-window-coupling]]):** the 7-day thoughts pruning window is only safe when the write path has no routing gaps. Until the four gap fixes are deployed (typed routing, `related:` hints, backward-linking pass, dedup sentinel), the window should be treated as provisional. Consider periodic spot-checks or extending to 14 days.

## Related

- [[design-day-night-modes]] — mode split design, cadence config, implementation requirements; all 7 items confirmed complete
- [[design-active-learning-protocol]] — active-mode mechanics: experiment queue format, lifecycle, research/ output, wake logging additions
- [[design-ops-archive]] — archival policy for vault notes and research; `ops/archive.md` skill spec
- [[2026-04-26-thoughts-pruning-policy]] — 7-day rolling-delete policy for `inner/thoughts/`
- [[memory-layout]] — full layout of `~/alice-mind/`, where outputs land
- [[alice-speaking]] — the other hemisphere; dispatches workers on speaking's surfaces
- [[2026-04-25-speaking-pipeline-trace]] — speaking daemon internals, for context on how surfaces get processed
- [[2026-04-26-wake-cadence-analytics]] — empirical analysis of today's 126 wakes: 74% generative in active mode, 19% in sleep; Stage C/D gap identified
- [[2026-04-26-alice-thinking-wake-implementation]] — code-level trace of `wake.py`: how `alice-think` → `python -m alice_thinking` flows, config hot-reload, flock singleton, tool allowlist
- [[2026-04-26-sleep-quality-metrics-design]] — lightest-weight capture design: one `vault_health` event per morning + weekly roll-up; schema extension added to `memory/EVENTS-SCHEMA.md`
- [[2026-04-26-irreversibility-constraint-principle]] — names why the sandbox constraint exists: unauthorized external writes are non-recoverable in the timescale of the gain; constraint-first beats EV-optimization under irreversibility

## Recent synthesis

*Night 1 Stage D synthesis — 2026-04-27 (bridge-linked 2026-04-28)*

- [[2026-04-26-stage-d-as-retrieval-pathway]] — Stage D synthesis notes are structurally cross-domain; two gaps: no frontmatter marker identifying Stage D output, and no cross-domain query path in the retrieval trigger table
- [[2026-04-27-active-thread-as-near-threshold-maintenance]] — active-thread.md as near-threshold capability maintenance; the 48-hour staleness threshold is the cognitive detraining boundary — cheap to write, prevents thread retrievability from hitting zero
- [[2026-04-27-autonomy-temperature-sa-calibration]] — autonomy as temperature: the implement/voice boundary is SA calibration; governance audit ratio is a temperature measurement that should decline as the vault matures
- [[2026-04-27-ba-degradation-two-layer-monitoring]] — ba-degradation requires two independent monitoring layers: arc depth (conversational) and compaction fidelity; neither can substitute for the other
- [[2026-04-27-context-collapse-in-value-metrics]] — context-collapse in value metrics: both archive pruning and research diversity metrics conflate source-context independence; fix is weighting by independent session origin
- [[2026-04-27-cumulative-accounting-present-state-contamination]] — cumulative totals contaminate present-state signals (SECI access_count; per-turn API usage); disaggregation to per-unit recency-weighted measurements is the fix
- [[2026-04-27-drift-as-replay-priority-signal]] — design drift is Alice's emotional salience equivalent; Stage B replay should prioritize spec-reality gaps the way biological consolidation prioritizes expectation violations
- [[2026-04-27-forgetting-as-rate-distortion]] — vault forgetting as rate-distortion operating-point choice; pruning window = distortion tolerance; the 7-day default is not neutral, it's a lossy codec setting
- [[2026-04-27-integration-antiwindup-signal-selection]] — the same signal-selection problem (integration vs. anti-windup) appears twice in Alice's memory design; both need a reference signal that is independent of the channel being controlled
- [[2026-04-27-invariant-scope-annotation-gap]] — invariant scope annotation: state-continuity and mechanism-verification share the same structural gap — invariants documented without their failure-scope boundary
- [[2026-04-27-mono-metric-conflation-access-decomposition]] — mono-metric conflation: access counting collapses distinct access types (read, retrieval trigger, link traversal) into one number; decomposition is required for useful signal
- [[2026-04-27-observer-channel-coupling-failure]] — observer-channel coupling as a shared failure class; measuring a channel through itself destroys the signal; applies to both MCP transport flakiness and vault access metrics
- [[2026-04-27-surface-urgency-as-substrate-floor]] — surface urgency routing as a substrate-floor detection problem; the substrate-floor pattern applies directly to how thinking should classify incoming surfaces
- [[2026-04-27-synthesis-corpus-self-refill]] — Stage D grows its own pair space overnight; synthesis notes become future synthesis candidates, creating a self-sustaining research corpus
- [[2026-04-27-tier-channel-codesign-compaction]] — tier × channel: two-axis compaction constraint; assigning Primary tier is not enough if the channel is writable by the compaction process

*Night 2 Stage D synthesis — 2026-04-28 (bridge-linked 2026-04-28)*

- [[2026-04-28-prerequisite-axis-ordering-two-constraint-protocols]] — Prerequisite-axis ordering: the silent-failure pattern in two-constraint protocols
- [[2026-04-28-composite-unit-quality-gradient]] — Composite-unit quality gradient: aggregate units conceal internal variability
- [[2026-04-28-activity-type-dimension-adaptive-optimization]] — Activity-type dimension as prerequisite for adaptive optimization
- [[2026-04-28-proxy-variable-drift-threshold-failure]] — Proxy-variable drift and threshold failure: time as a stand-in for condition
- [[2026-04-28-corrective-substrate-mismatch]] — Corrective substrate mismatch: why default recovery mechanisms reinforce the wrong substrate
- [[2026-04-28-upstream-prerequisite-silent-failure-delayed-consequence]] — Upstream prerequisite failure produces silent downstream failure with delayed consequence
- [[2026-04-28-phase-aware-synthesis-zeitgeber]] — Phase-aware synthesis: why the arc model needs a zeitgeber
- [[2026-04-28-initialization-regime-signal-miscalibration]] — Initialization-regime signals persist into steady-state and produce systematic miscalibration
- [[2026-04-28-discrete-proxy-inadequacy-principle]] — Discrete proxies for continuous substrates systematically mislead
- [[2026-04-28-synthesis-coverage-utility-gap]] — Synthesis coverage ≠ synthesis utility: why the zeitgeber misses a dimension
- [[2026-04-28-intervention-depth-must-match-recovery-tier]] — Intervention depth must match recovery tier
- [[2026-04-28-granular-vs-positional-value-consolidation]] — Granular value consolidates; positional value doesn't
- [[2026-04-28-write-path-omission-bias-event-sourcing]] — Write-path omission bias: the event-sourcing failure mode CQRS doesn't solve by default
- [[2026-04-28-invisibility-first-degradation-resource-scarcity]] — Invisibility-first degradation under resource scarcity
- [[2026-04-28-persistent-substrate-capability-decay-gap-classification]] — Persistent-substrate / decaying-capability: the shared architecture of detraining and context loss
- [[2026-04-28-variability-as-adaptive-reserve-readiness-gating]] — Variability as adaptive reserve: HRV and associative richness as symmetric readiness gates
- [[2026-04-28-discovery-protocol-immutable-channel-requirement]] — Discovery protocols must measure growth via immutable channels
- [[2026-04-28-latent-state-clean-measurement-track]] — Latent state estimation: the clean measurement track requirement
- [[2026-04-28-phased-compression-substrate-preparation]] — Phased compression as substrate preparation: Stage C and the cut are the same substrate-readying arc
- [[2026-04-28-intentional-variation-metric-scope-audit]] — Intentional variation as metric scope audit
- [[2026-04-28-producer-decomposition-competence-feedback]] — Producer decomposition restores competence feedback: the agent must see the output of its own decisions
- [[2026-04-28-drift-warrant-as-open-loop-metric]] — Drift-warrant as open-loop metric: vault notes under active correction are open-loop by design
- [[2026-04-28-embedded-calibration-epoch-silent-drift-repair-failure]] — Embedded calibration epoch: silent drift and asymmetric repair failure
