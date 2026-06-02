# alice_thinking

**TBD** — Thinking is the background generative hemisphere. It runs on a 5-minute cadence, reads the `cortex-memory/` vault and `inner/notes/` for context, runs experiments / drafts designs / synthesizes research, and surfaces actionable findings to Speaking via `inner/surface/`. Source: `src/alice_thinking/`.

Phase 5 (2026-06-02) of the memory-worker extraction made thinking single-mode (always generative). Mechanical vault grooming — inbox drain, atomize, archive, dedupe, recombination — moved to the `alice-memory-worker` service. See `src/alice_thinking/memory_worker/` and the `alice-memory-worker` project note in `cortex-memory/projects/`.

Filled in as PRs touch the package. Points worth covering when this stub becomes real prose:

- wake loop and cadence (single-mode, 5-min)
- task-type preempts: design commission, conflict resolution
- vault read protocol and act-on-this surfacing
- co-existence with the memory worker via `vault_lock`
