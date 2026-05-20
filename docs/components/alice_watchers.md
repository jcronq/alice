# alice_watchers

**TBD** — repository / event watchers. These poll external systems (GitHub, calendar, Signal-adjacent surfaces, etc.) and emit notes into `inner/notes/` for Thinking to triage. Source: `src/alice_watchers/`.

Filled in as PRs touch the package. Points worth covering when this stub becomes real prose:

- per-watcher polling cadence and dedup strategy
- note schema each watcher emits
- trusted-author / `attempt-issue-fix` handoff to Speaking
- failure modes and back-off behavior
