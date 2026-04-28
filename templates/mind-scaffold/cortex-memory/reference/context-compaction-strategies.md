---
title: Context compaction strategies — trade-offs
aliases: [compaction strategies, context overflow strategies, context window strategies]
tags: [reference, agent-design, research]
created: 2026-04-25
---

# Context compaction strategies — trade-offs

> **tl;dr** Six strategy families cover the space of context overflow handling, each with distinct fidelity/cost/latency/failure-mode trade-offs.

Extracted from [[context-window-pressure-survey-2026]]. That note covers per-framework implementation detail; this note covers the underlying strategy families and their trade-offs. For best-practice patterns and synthesis of what to actually do, see [[context-compaction-best-practices]].

---

## Strategy family taxonomy

### 1. Sliding window / message truncation

**Mechanism**: keep last N messages or last K tokens; drop oldest.

**When it works**: stateless Q&A, single-turn tasks, customer service where each turn is independent.

**When it fails**: anything requiring multi-turn context.

| Dimension | Rating |
|---|---|
| Fidelity | Low (drops are total — no recovery) |
| Cost | Very low (no extra LLM calls) |
| Latency overhead | Near-zero |
| Implementation complexity | Trivial |
| Failure mode | Silent amnesia — agent doesn't know what it forgot |

**CMU finding**: models show 23% performance degradation when context utilization exceeds 85% — trigger should fire earlier than most implementations set it.

---

### 2. Summarization / compaction

**Mechanism**: fire a secondary LLM call to summarize older context into a compressed representation; substitute summary for original messages.

| Variant | How it works | Best for |
|---|---|---|
| Rolling summary | Accumulates incrementally; each compaction adds to the prior | Conversation agents |
| Full re-summarize | Entire history re-summarized each time | Higher fidelity, higher cost |
| Anchored summary (Factory.ai) | Delta-summarize newly dropped span, merge with persisted anchor | Long coding sessions |
| Structured summary | Explicit sections: intent, files, decisions, state, next steps | Task-oriented agents |
| Domain-tuned (ACON) | Compressor prompt optimized via failure analysis on paired trajectories | Known task types |

| Dimension | Rating |
|---|---|
| Fidelity | Medium–High (depends on summary quality) |
| Cost | Medium (one extra LLM call per compaction) |
| Latency overhead | High at compaction point; normal otherwise |
| Failure mode | Summarization drift — low-frequency details vanish after multiple passes |

**Key benchmark**: Factory.ai structured summaries scored 3.70/5.0 vs. Anthropic SDK 3.44 vs. OpenAI 3.35, biggest gap in accuracy (4.04 vs. 3.43) due to retaining precise technical details like file paths.

**Worst preserved dimension**: artifact tracking (2.19–2.45/5.0) — *no compaction method handles "which files have I touched" well* without explicit structured tracking.

---

### 3. Hierarchical / virtual context paging (MemGPT-style)

**Mechanism**: model explicitly manages tiered memory via tool calls — decides what to evict from working context into external storage and what to load back in.

| Dimension | Rating |
|---|---|
| Fidelity | High (agent controls what's preserved) |
| Cost | Medium-high (more tool calls; external storage lookups) |
| Failure mode | Silent eviction errors — agent pages out something it silently needed |

**Key weakness**: eviction decisions made under context pressure by the same LLM. Suboptimal choices yield "slightly worse responses" with no exception or log.

---

### 4. Sub-token / token compression (LLMLingua family)

**Mechanism**: compress individual tokens using a small classifier model — not summarization, actual token dropping. Up to 20x reduction.

- **LLMLingua** (EMNLP 2023): coarse-to-fine, up to 20x
- **LLMLingua-2** (ACL 2024): data-distilled from GPT-4, BERT-level encoder, 3–6x faster
- **LongLLMLingua** (ACL 2024): query-aware, 21.4% perf improvement with 4x fewer tokens

| Dimension | Rating |
|---|---|
| Fidelity | High (keeps original content) |
| Cost | Low-medium (small classifier) |
| Failure mode | Classifier errors drop load-bearing tokens; hard to debug because text looks coherent |

**Best use case**: long static documents in context (RAG chunks, codebase context). Poor fit for conversation history where compression artifacts accumulate.

---

### 5. Retrieval-augmented context (RAG-based memory)

**Mechanism**: store history in vector index; retrieve only the semantically relevant subset each turn.

| Dimension | Rating |
|---|---|
| Fidelity | Medium (retrieval misses happen) |
| Cost | Low ongoing; medium setup |
| Latency overhead | Low–Medium (~50–200ms ANN lookup) |
| Failure mode | Silent retrieval miss — relevant context exists but isn't retrieved |

**Mem0 reported**: 91% lower P95 latency vs. full-context; 90% token savings; 26% accuracy improvement over OpenAI's built-in memory.

---

### 6. Multi-agent context isolation

**Mechanism**: route subtasks to specialized sub-agents; each has a clean context window; orchestrator maintains task-level summary only.

| Dimension | Rating |
|---|---|
| Fidelity | High per-subtask; orchestrator summary introduces loss |
| Cost | High (multiple context windows active) |
| Failure mode | Orchestrator context grows unbounded if subtask summaries are verbose |

**Chain of Agents** (NeurIPS 2024): worker chain with manager synthesis; O(n·k) complexity; up to 10% over RAG/truncation baselines on long QA.

---

## The compaction turn problem and best practices

See [[context-compaction-best-practices]] — what gets lost (artifact trail, continuation quality, precise technical details), the five best-practice patterns, synthesis, and open questions.

---

## Novel and frontier techniques

### RL-trained memory agents (MemAgent, arXiv:2507.02259)

End-to-end RL training to optimize memory operations. Reads one chunk at a time, rewrites a fixed-length memory buffer after each chunk. Fixed buffer size = linear cost scaling. Extrapolates from 8K training to 3.5M tokens with <5% loss. The RL model discovers *which information to compress lossily vs. copy verbatim* — a distinction handcrafted prompts frequently get wrong.

### KV-cache-native memory (MemArt, OpenReview:YolJOZOGhI)

Stores the KV cache state rather than text. Retrieval operates in the model's native representation space (attention scores between current KV state and stored KV blocks). Eliminates the embedding gap. 90x fewer prefill tokens, 11% accuracy improvement. Downside: not portable across model versions.

### Memory pointer pattern (IBM Research, arXiv:2511.22729)

For scientific/engineering workflows where tool outputs are fundamentally large, summarization loses precision. Replace data with references:

```json
{"type": "tool_result", "data_ref": "uuid:3fa8b2...", "shape": [128, 128, 128]}
```

16,000x token reduction in Materials Science benchmark. Full-context baseline used 20.8M tokens and failed; pointer approach used 1,234 tokens and succeeded.

### Sleep-time consolidation

Background consolidation during agent idle time: episodic notes promoted to semantic memory, duplicates merged, stale entries flagged. Arrives at each active turn with cleaner, denser semantic memory — reducing retrieval noise and decreasing compaction frequency. Prevention, not treatment. Alice's thinking-hemisphere design is an instance of this pattern.

### Multi-granularity indexing (RMM, ACL 2025)

Store at utterance-level, turn-level, and session-level simultaneously. RL-trained retrospective reflection uses which retrieved memory was actually cited as a training signal — cited memories get higher retrieval scores, uncited ones lower. Closes the retrieval quality feedback loop.

---

## Key papers

- **ACON** (arXiv:2510.00615, NeurIPS 2025) — failure-analysis-driven guideline optimization; 26–54% token reduction
- **MemAgent** (arXiv:2507.02259) — RL-trained chunked memory; 3.5M-token generalization from 8K training
- **Chain of Agents** (arXiv:2406.02818, NeurIPS 2024) — worker chain for long docs; O(n·k) complexity
- **LLMLingua-2** (arXiv:2403.12968, ACL 2024) — BERT-level token compression, 3–6x speedup
- **A-MEM** (arXiv:2502.12110, NeurIPS 2025) — Zettelkasten-style backward-linking memory
- **RMM** (arXiv:2503.08026, ACL 2025) — prospective + retrospective reflection; >10% on LongMemEval
- **MemArt** (OpenReview:YolJOZOGhI) — KV-cache-native memory; 90x prefill reduction
- **Factory.ai eval** (2025) — probe-based production evaluation; artifact tracking universally worst

---

## Related

- [[context-compaction-best-practices]] — what gets lost + five best-practice patterns + synthesis + open questions (extracted from this note)
- [[context-window-pressure-survey-2026]] — the framework-by-framework survey this was extracted from
- [[design-context-compaction]] — Alice's compaction design
- [[design-unified-context-compaction]] — full v3 unified context design
- [[llm-agent-memory-survey-2025]] — broader memory taxonomy
- [[design-context-persistence]] — Alice's context persistence design
- [[2026-04-25-compaction-event-observability]] — design sketch for making Alice's compaction observable
