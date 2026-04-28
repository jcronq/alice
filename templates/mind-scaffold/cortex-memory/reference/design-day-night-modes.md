---
title: Design — Active/Sleep Mode Split for Thinking Alice
aliases: [active-sleep-modes, day-night-modes, rem-mode, active-learning-mode, thinking-modes]
tags: [reference, design]
created: 2026-04-25
---

# Design — Active/Sleep Mode Split for Thinking Alice

> **Naming note (Owner, 2026-04-25 21:00):** the user-facing mode names are **active** and **sleep**. "REM" is fine as a colloquial / biology-framing alias for sleep. "Day" and "night" are kept here as historical aliases (filename + earlier prose) but should not be used for new writing. Config keys use `rem_cadence_minutes` and `active_cadence_minutes` (shipped 2026-04-25).

> **tl;dr** v3 — UPDATED 2026-04-25 21:49 EDT. Two modes: sleep/REM (23:00–07:00) for vault grooming at 5-min cadence; active (07:00–23:00) for experiments at **5-min cadence** (Owner overrode 30 → 5 on 2026-04-25; `alice.config.json` updated). Both modes now run at 5/5. Cadence config in `alice.config.json` (`thinking.rem_cadence_minutes` + `thinking.active_cadence_minutes`); s6 run-script detects hour at each iteration. See [[design-active-learning-protocol]] for active-mode experiment mechanics.

## The Biology

Sleep science maps cleanly onto thinking's two workloads:

- **Slow-wave (NREM)**: synaptic pruning — consolidate, compress, drop noise, retain signal.
- **REM**: creative recombination — pattern integration, hypothesis generation, connecting disparate memory.

For thinking Alice:
- **REM mode (night)**: vault grooming, linking, note promotion, frontmatter normalization. Low-creativity, high-reliability consolidation. What thinking currently does 24/7.
- **Active-learning mode (day)**: experiments, code archaeology, data synthesis, design proposals, wandering. High-curiosity, open-ended learning. What thinking should do during waking hours.

The overlap analysis (2026-04-25, wake 154) confirmed the vault reached stable equilibrium around wake 90 today. 37% of wakes found nothing; 28% were cosmetic. That's the REM gear running at noon. Wrong gear for the wrong time.

---

## Mode Boundaries

- **REM mode**: 23:00–07:00 local time.
- **Active-learning mode**: 07:00–23:00 local time.

Detection: check local hour at wake start. If `hour < 7 or hour >= 23` → REM. Else → active-learning.

---

## Cadence by Mode

Config (as of 2026-04-25 21:49 EDT, Owner override):

```json
{
  "thinking": {
    "rem_cadence_minutes": 5,
    "active_cadence_minutes": 5
  }
}
```

- **REM at 5 min**: appropriate when notes inbox might have items from overnight Speaking turns; tight cadence catches activity quickly. Apply adaptive backoff when vault is stable (after 3 empty wakes → extend to 30 min, cap at 60 min); reset to 5 min on any notes-inbound or new surface.
- **Active at 5 min** *(changed 2026-04-25 21:49 EDT, was 30 min)*: Owner prefers higher cadence. Original rationale for 30 min ("each wake should accomplish a meaningful chunk; a 5-min tick produces one tool call and closes") is preserved here as rationale context, but the actual operating value is 5. Practical implication: multi-wake experiment pacing shifts — each wake does a smaller increment. `budget_wakes` framing may need revisiting; for now use shorter per-wake increments and let experiments naturally span more wakes.

Run script (`s6/alice-thinker/run`) needs updating to: (a) read two cadence values from config, (b) check current hour to pick which one. Speaking implements.

---

## REM Mode (Night)

What it does: the current protocol, unchanged. Step 1 wake file → Step 2 drain inbox → Step 3 groom one vault op → Step 4 surface if sharp → Step 5 close.

Changes vs today:
1. Apply adaptive backoff when vault is genuinely stable (not just incrementing wake counters).
2. Drop ideas that arise during grooming into `inner/ideas.md` for day-mode pickup.
3. Wake file frontmatter: `mode: rem`.

---

## Active-Learning Mode (Day)

Priority tiers (Owner's explicit hierarchy, 2026-04-25):
1. **Active problems** — highest priority; when Owner has something in flight, serve it.
2. **Topics of interest** — strong preference when no active problem; not exclusive to Owner-topics.
3. **Free wandering** — explicitly permitted even when off-topic.

Full experiment mechanics — queue format, lifecycle, research/ output, wake logging — in [[design-active-learning-protocol]].

---

## Implementation Requirements

Speaking implements. Thinking proposes.

1. **`alice.config.json`** ✅ — `rem_cadence_minutes: 5` and `active_cadence_minutes: 5` present; legacy `cadence_minutes` key absent. Verified 2026-04-26 01:12.
2. **`s6/alice-thinker/run`** ✅ — reads both values from config; hour-check selects mode (`hour < 7 || hour >= 23` → REM). Verified 2026-04-26 01:12.
3. **`directive.md`** ✅ — directive now has full mode-detection protocol (wake file `mode:` field + hour fallback) and active/sleep branch steps. Verified 2026-04-26 01:12.
4. **`inner/ideas.md`** ✅ — created wake 162 (2026-04-25 14:40 EDT). Seeded and in active use.
5. **`cortex-memory/research/`** ✅ — created wake 162 (2026-04-25 14:40 EDT).
6. **`cortex-memory/index.md`** ✅ — `## Research` section added wake 162.
7. **Thinking prompt** ✅ — directive carries mode-detection logic + full active-learning step sequence (read ideas queue → run experiment → write research/ → surface actionable). Verified 2026-04-26 01:12.

**All 7 implementation items complete.** Design is fully live as of 2026-04-26.

---

## Questions — Resolved (2026-04-25 14:24 EDT)

1. **Time boundaries** ✓ — 23:00–07:00 for REM (v2 corrected from 23:00–08:00).
2. **Grooming in day mode** — Not explicitly addressed. Working assumption: inbox drain always on; opportunistic vault grooming during active-learning wakes is OK when a research note touches a vault note directly ("groom as you go"). Full-pass grooming stays night-only. Revisit if day wakes start running long.
3. **Ideas queue seeding** ✓ — Thinking self-populates from day one. Owner routes requests through Speaking → notes → thinking. No direct file editing by anyone else.
4. **Generative artifacts** ✓ — Full toolkit confirmed: read, synthesize, document, produce. Code-on-paper (as text/prose demonstrating an idea) is fine. Execution constraints still apply (sandbox above).
5. **Compute budget signal** — Not addressed. Trust per-experiment `budget_wakes` cap for now.
6. **No day→night handoff protocol** ✓ — Night just grooms whatever day wrote. No `inner/day-output/` directory. Day writes directly to `cortex-memory/` (research/ for investigation artifacts, reference/ or projects/ for durable findings). Zero-protocol is the protocol.
7. **Active-problems queue ownership** ✓ — Thinking owns `inner/ideas.md` exclusively. Speaking routes via notes. Owner via Signal → Speaking → notes → thinking.
8. **Sandbox constraint** ✓ — Explicit (section above). Applies equally to day and night modes.

---

## Compatibility with v3 Unified Context

This design runs on top of the v3 implementation (confirmed implemented 2026-04-24 23:14). Day-mode thinking runs the same way as current: `alice-think` → `claude -p` → reads directive → runs steps → surfaces via `inner/surface/`. No changes to speaking daemon needed. The `send_message` tool, unified context, and compaction all remain speaking's domain. Thinking's output channel is still `inner/surface/` only.

---

## Dependencies

*Declared per [[2026-04-27-implicit-correctness-inheritance-design-principle]]: formal listing of foreign invariants this design inherits.*

| Dependency | Invariant assumed | Failure mode if invariant breaks | Detection signal |
|---|---|---|---|
| **`wake.py` local-time injection** | `wake.py` injects a DST-aware `Current local time` header at the top of every wake prompt, from which mode is computed | If the header is absent or uses UTC instead of local time, the hour comparison fires in the wrong timezone — active mode silently extends into sleep hours or vice versa | Wake files should always have `mode:` set; if a wake file lands with the wrong mode relative to wall clock, it's a DST/injection failure |
| **`alice.config.json` cadence fields** | `thinking.rem_cadence_minutes` and `thinking.active_cadence_minutes` are present, readable, and correct; `s6/alice-thinker/run` reads them at each iteration | If config is malformed or key is renamed, s6 falls back to a default cadence (or crashes); mode-specific cadence tuning silently reverts | s6 should log the config it reads at startup; validate presence of both keys; fail loudly on missing keys rather than silently defaulting |
| **Local system clock** | System clock is set correctly and NTP-synchronized | Mode-boundary misfires during DST transitions or if clock drifts; a 23:01 wake classified as active | NTP drift >60s should trigger alert in any production environment; for Alice, the container clock inheriting host clock is the typical path |

## Related

- [[alice-speaking]] — speaking hemisphere; owns the daemon and dispatches workers
- [[design-unified-context-compaction]] — the v3 design this runs on top of
- [[memory-layout]] — where research notes live vs reference vs dailies
- [[2026-04-25-sleep-architecture-design]] — extends sleep mode with three stage types (Consolidation / Downscaling / Recombination); surfaced 2026-04-25 for Speaking review
- [[2026-04-26-stage-cd-implementation-gap]] — investigation confirming Stage C/D have never run; `thinking-bootstrap.md` was not updated after design approval
- [[design-thinking-capabilities]] — unified operational envelope: synthesizes this note + active-learning protocol + ops-archive into a single reference
- [[alice-config]] — full `alice.config.json` schema; cadence fields (`rem_cadence_minutes`, `active_cadence_minutes`) live in the `thinking` section and are read by the s6 loop at each iteration
- [[2026-04-26-alice-sleep-neuroscience-analog]] — deeper neuroscience analysis: why "hemispheres" is the wrong analogy (it's systems consolidation, not lateralization); stage B/C/D mapped to NREM-2/SWS/REM with full justification; also identifies three gaps (emotional tagging, context reinstatement, prospective memory)

## As of

v1 drafted 2026-04-25 wake 157 (14:09 EDT). v1 tl;dr tightened at wake 158 (14:15 EDT). **v2 promoted to READY TO IMPLEMENT** at wake ~160 (14:27 EDT) after Owner clarifications: time boundary corrected to 23:00–07:00; active-problems queue moved from directive.md to inner/ideas.md (Thinking-owned); sandbox constraint made explicit; all open questions resolved. Sources: consumed notes `2026-04-25-182540-design-day-night-owner-clarifications.md` + `2026-04-25-182628-design-day-night-sandbox-constraint.md`. **v3 cadence update 2026-04-25 22:15 EDT**: Owner overrode active_cadence_minutes 30 → 5 (2026-04-25 21:49 EDT); both modes now 5/5. Source: `inner/notes/2026-04-26-015002-cadence-config-change.md`.

## Recent synthesis

*Night 1 Stage D synthesis — 2026-04-27 (bridge-linked 2026-04-28)*

- [[2026-04-26-smith-predictor-model-accuracy]] — Stage D acts as a Smith predictor for vault accuracy; T3/T4 quality output is worse than no Stage D — it inserts wrong predictions, causing Speaking to over-correct
- [[2026-04-27-mechanism-context-verification-gap]] — mechanism verified in isolation may fail in context; Stage D's spaced-synthesis design has the same verification gap as unit tests that never run in integration
- [[2026-04-27-phantom-threshold-pathway-absence]] — phantom threshold: stages C/D that never run look like sub-threshold load; pathway absence is the actual failure mode, not low stimulus
- [[2026-04-27-stage-d-night1-quality-sample]] — Night 1 morning quality sample: 0% forced rate, 60% insight rate, ahead of baseline; tag-disjoint filter working; early-bloom SA convergence candidate
- [[2026-04-27-stamp-file-as-junk-volume-prevention]] — stamp file prevents duplicate §4 reads; generalizes to any I/O system where re-processing is costly

*Night 2 Stage D synthesis — 2026-04-28 (bridge-linked 2026-04-28)*

- [[2026-04-28-prerequisite-axis-ordering-two-constraint-protocols]] — Prerequisite-axis ordering: the silent-failure pattern in two-constraint protocols
- [[2026-04-28-reference-point-drift-metric-inversion]] — Reference point drift as metric inversion: static anchors fail at both ends of a transition
- [[2026-04-28-resolution-proportionate-escalation-probe-tiers-dual-process]] — Resolution-proportionate escalation: probe-tier and dual-process structures share a common framework
- [[2026-04-28-activity-type-dimension-adaptive-optimization]] — Activity-type dimension as prerequisite for adaptive optimization
- [[2026-04-28-proxy-variable-drift-threshold-failure]] — Proxy-variable drift and threshold failure: time as a stand-in for condition
- [[2026-04-28-corrective-substrate-mismatch]] — Corrective substrate mismatch: why default recovery mechanisms reinforce the wrong substrate
- [[2026-04-28-layer-dominance-metric-interpretability]] — Layer dominance and metric interpretability: when a valid metric measures the wrong layer
- [[2026-04-28-post-actuator-sampling]] — Post-actuator sampling: metrics only interpretable after the correct activation event
- [[2026-04-28-phase-aware-synthesis-zeitgeber]] — Phase-aware synthesis: why the arc model needs a zeitgeber
- [[2026-04-28-internal-consistency-blindness]] — Internal-consistency blindness: why standard health metrics miss the signal at system boundaries
- [[2026-04-28-contingent-substrate-immunity-resilience-gap]] — Contingent substrate immunity: behavioral zero-drift guarantees require substrate independence
- [[2026-04-28-exercise-substitution-silence-signal-gap]] — Exercise substitution is a silence-signal problem requiring pre-event registry
- [[2026-04-28-synthesis-coverage-utility-gap]] — Synthesis coverage ≠ synthesis utility: why the zeitgeber misses a dimension
- [[2026-04-28-substrate-rollback-vs-adaptive-state-correction]] — Substrate rollback vs. adaptive-state correction: two recovery regimes with different risk profiles
- [[2026-04-28-granular-vs-positional-value-consolidation]] — Granular value consolidates; positional value doesn't
- [[2026-04-28-invisibility-first-degradation-resource-scarcity]] — Invisibility-first degradation under resource scarcity
- [[2026-04-28-persistent-substrate-capability-decay-gap-classification]] — Persistent-substrate / decaying-capability: the shared architecture of detraining and context loss
- [[2026-04-28-substrate-separation-critical-resource]] — Substrate separation for critical resources: two failure modes of colocation
- [[2026-04-28-variability-as-adaptive-reserve-readiness-gating]] — Variability as adaptive reserve: HRV and associative richness as symmetric readiness gates
- [[2026-04-28-discovery-protocol-immutable-channel-requirement]] — Discovery protocols must measure growth via immutable channels
- [[2026-04-28-adaptive-feedback-anchored-to-read-model-drift]] — Adaptive feedback anchored to read-model drift: when the control loop reads a stale projection
- [[2026-04-28-threshold-alarm-causal-decomposition]] — Threshold alarm ambiguity: causal decomposition before response
- [[2026-04-28-proxy-silence-as-false-health-signal]] — Proxy silence as false health signal: invisible accumulation across fitness, vault, and Alice-architecture
- [[2026-04-28-drift-warrant-as-open-loop-metric]] — Drift-warrant as open-loop metric: vault notes under active correction are open-loop by design
- [[2026-04-28-phase-goal-annotated-threshold-sufficiency]] — Phase-goal-annotated threshold sufficiency
- [[2026-04-28-independent-spec-shared-resource-conflict]] — Independent spec, shared resource: constraint conflict is invisible at spec time
- [[2026-04-28-embedded-calibration-epoch-silent-drift-repair-failure]] — Embedded calibration epoch: silent drift and asymmetric repair failure
