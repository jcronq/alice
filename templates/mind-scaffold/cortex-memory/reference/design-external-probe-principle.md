---
title: design-external-probe-principle
aliases: [external-probe-principle, internal-consistency-blindness, out-of-band-health-probe]
tags: [design, systems-design, observability, reliability, alice-mind, vault-health]
status: active
created: 2026-04-28
---

# Design: External Probe Principle

> **tl;dr** Any health metric that only interrogates internal state will reliably miss failure modes that live at the system-environment boundary. Every subsystem needs at least one out-of-band probe that bypasses internal bookkeeping and queries external ground truth.

---

## The failure class

Systems can be internally self-consistent while diverging from external reality. When they are, every native health metric passes — because the problem isn't inside the bookkeeping, it's at the boundary between the system and its environment.

Concrete instances:

| System | Internal metric (passes) | What it misses |
|--------|--------------------------|----------------|
| Vault | `broken_wikilinks = 0` | Over-linked graph that satisfies the constraint by adding non-semantic links — no cluster structure, hairball graph |
| Vault | `orphan_notes = 0` | Notes that link into the graph but are never traversed by organic grooming — structural presence ≠ reachability |
| Zone-2 HR zone | HR formula returns a number | Parameters computed from pre-cut baseline; physiology has shifted; formula runs cleanly on stale inputs |
| Code coverage | Branch exists, links resolve | Branch is dead under the real input distribution; the structural check doesn't test reachability |

The failure class was named in [[2026-04-28-internal-consistency-blindness]] (Stage D synthesis, Night 2). Source paper: [[2026-04-28-connected-but-unreachable-convergence]] (structural connectivity ≠ reachability) × [[2026-04-28-parameter-staleness-as-invisible-accumulation]] (parameter presence ≠ validity).

---

## The probe requirement

For each failure class, the probe must stand *outside* the system's value chain and query reality directly:

- **Graph structure:** modularity score on the topical subgraph (excluding dailies). Not a link count — a structural test. See [[design-graph-cluster-quality]].
- **Note reachability:** access-pattern audit (which notes does organic grooming actually reach vs which notes are structurally linked). Dormant-neighbor fraction.
- **HR zone validity:** talk test + MAF ceiling. Bypasses the Karvonen formula entirely — asks the body, not the arithmetic.
- **Code reachability:** input-distribution simulation. Does the current production traffic actually produce inputs that trigger the branch?

The probe form varies; the requirement is constant: **external reference, not internal consistency check.**

---

## Alice-specific applications

1. **vault_health modularity field** — `broken_wikilinks = 0` / `orphan_notes = 0` are passing grades on an over-linked graph. The `graph_modularity` field (pending Speaking implementation per [[design-graph-cluster-quality]]) is the external probe — it asks "does the graph form distinct clusters?" rather than "does every link resolve?"

2. **Stage C hub audit** — the earned/keyword/courtesy/stale classification is an access-semantics probe: "does the linking note actually *need* the hub, or just name it?" This bypasses the link-existence check (internal) and queries the semantic dependency (external).

3. **Compaction integrity** — context-summary §1 "Active threads" can look intact while carrying degraded specificity (stale conditions summarized to vague labels). Out-of-band probe: Thinking comparing §1 items against the vault's own dated records.

4. **Speaking turn health** — speaking.log `signal_turn_end` schema reports duration and error, but if `outbound` is absent, the arc-enrichment step falls back to a cross-file lookup. Silent fallback = internal consistency. The external probe is checking that the output actually reached the recipient (receipt confirmation, not just no-error log).

---

## Corollary: silence as false health signal

Absence of error is not evidence of correctness when the probe measures the wrong thing. Any monitoring architecture that lacks an external probe for a given failure mode will produce structured silence — regular health reports that say "fine" while the boundary diverges. See [[2026-04-28-proxy-silence-as-false-health-signal]].

---

## Related

- [[2026-04-28-internal-consistency-blindness]] — origin synthesis; ghost-branch / RIF / stale-parameter instances
- [[design-graph-cluster-quality]] — vault graph modularity as the external probe for linking quality
- [[design-linking-discipline]] — companion authoring rule; prevents over-linking before the audit runs
- [[2026-04-28-proxy-silence-as-false-health-signal]] — silence-as-health-signal corollary
- [[2026-04-28-drift-warrant-as-open-loop-metric]] — parameter drift as open-loop metric failure
