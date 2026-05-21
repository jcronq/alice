"""Alice State Machine dispatcher — orchestrates the per-task lifecycle.

See cortex-memory/research/2026-05-12-state-machine-v2-github-substrate.md
for the full design. v0 here is the minimum dispatcher that exercises the
GitHub substrate pipeline end-to-end: poll → trust-filter → comment.
"""
