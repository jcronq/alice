---
title: Context compaction — best practices
aliases: [compaction best practices, compaction patterns, context overflow best practices]
tags: [reference, agent-design, research]
created: 2026-04-25
---

# Context compaction — best practices

> **tl;dr** Five patterns maximize compaction fidelity: structured-section summaries, task-boundary triggers, anchored incremental summarization, explicit artifact side-channels, and observable compaction turns.

Extracted from [[context-compaction-strategies]] to separate the taxonomy (what the strategies are) from the practice (how to use them well). The synthesis here is the actionable layer on top of the six strategy families.

---

## The compaction turn problem

The core challenge: a compaction that fires mid-task must preserve enough state for the agent to continue as if nothing happened, while the user experiences zero discontinuity.

### What gets lost most (Factory.ai probe eval, 36,611 production messages)

1. **Artifact trail** (worst: 2.19–2.45/5): which files created/modified/read; which tests run; which commands executed. Generic summarization doesn't naturally capture task state.
2. **Continuation quality**: generic summaries produce "fresh start" posture rather than "mid-task continuation."
3. **Precise technical details**: file paths, variable names, exact error messages. Preservable if the summarization prompt specifically asks — defaults treat them as low-salience.

### Best-practice patterns

**Pattern 1 — Pause and inspect (Anthropic API)**

```python
if response.stop_reason == "compaction":
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": pinned_context})
    response = client.beta.messages.create(...)
```

Cleanest approach: boundary is explicit, orchestrator controls what carries forward.

**Pattern 2 — Structured summary with explicit sections**

```
Summarize. Include:
- SESSION INTENT: The main goal being pursued
- FILES MODIFIED: Exact paths of all files created or changed
- DECISIONS MADE: Key technical decisions and rationale
- CURRENT STATE: Completed and remaining
- NEXT STEPS: Immediate next action
```

**Pattern 3 — Anchored incremental summary (Factory.ai)**

```
anchor_summary = previous_anchor + summarize(newly_dropped_span)
```

Two thresholds: `T_max` (trigger) and `T_retained` (post-compaction target). Avoids re-summarizing already-summarized content that causes compounding loss.

**Pattern 4 — ACON guideline optimization**

For long-horizon tasks in known environments: collect paired trajectories (full context succeeds, compressed fails), run an LLM to diagnose what was lost, update the compression prompt. Achieves 26–54% token reduction while maintaining performance.

**Pattern 5 — Tool result clearing**

Before compaction, clear tool results no longer needed (old test runs, intermediate reads) while preserving final results. Anthropic's context editing API supports `tool_result_clearing`. Reduces tokens without semantic loss.

---

## Synthesis: what the best implementations do

1. **Use structured summaries, not prose summaries.** Generic summarization loses task-relevant signal. Define explicit schema for what must be preserved: intent, artifacts, decisions, state, next steps.

2. **Separate the compaction trigger from the response trigger.** Fire compaction at natural task boundaries (end of a subtask, between tool call chains) not purely by token count.

3. **Never compress twice without anchoring.** Incremental summarization without anchoring causes compounding information loss. Delta-summarize only the newly dropped span and merge with a persisted anchor.

4. **Track artifacts explicitly, not narratively.** Maintain a structured side-channel (`modified_files.json`, `session_state.md`) outside the summarized context that persists independently. Anthropic's NOTES.md-in-context pattern is an instance of this.

5. **Make the compaction turn observable.** Log every compaction: trigger token count, summary content, post-compaction context size. This is how you detect silent failures before they surface as confusing agent behavior. See [[2026-04-25-compaction-event-observability]] for Alice's implementation design.

6. **For large tool outputs: use references, not summaries.** When results are fundamentally large, summarization loses precision. Store externally; pass references.

7. **Prefer offline consolidation over on-demand compaction where latency allows.** Background consolidation produces cleaner context at every turn.

---

## Open questions / frontiers

1. **Cross-session compaction**: within-session overflow is mostly solved; cross-session state (episodic → semantic promotion) is still mostly manual.
2. **Compaction fidelity benchmark**: no standardized open benchmark for "did the agent retain everything it needed?" Factory's probe eval is the closest but is internal.
3. **Multi-agent shared context**: who controls compaction when multiple agents share memory? Race conditions on summarization largely unsolved.
4. **Adaptive compression ratio**: current systems use fixed thresholds; right threshold varies by task type. ACON's task detection points in the right direction.
5. **KV-cache persistence at inference layer**: MemArt shows gains from native representation space; as APIs expose more KV-cache primitives, this may become the dominant approach.

---

## Related

- [[context-compaction-strategies]] — the strategy taxonomy this was extracted from; six families with fidelity/cost/failure-mode ratings
- [[context-window-pressure-survey-2026]] — per-framework survey; the original source material
- [[design-context-compaction]] — Alice's compaction design
- [[design-unified-context-compaction]] — full v3 unified context design
- [[2026-04-25-compaction-event-observability]] — design sketch for making Alice's compaction observable (synthesis point 5 implemented)
- [[llm-agent-memory-survey-2025]] — broader memory taxonomy
