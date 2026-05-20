# alice docs

Living documentation for the `alice` runtime. The goal is simple: anything we ship should be findable here, and anything we change should leave the docs accurate when the PR merges.

## Layout

- `architecture/` — system-level structure: how Speaking, Thinking, the state machine, watchers, and the vault fit together.
- `components/` — per-package documentation. One file per top-level package under `src/alice_*`. Stub-first; filled in as the package gets touched.
- `user/` — operator-facing material: CLI usage, configuration knobs, deployment, runbooks.
- `designs/` — long-form design notes (existing). Living history of in-flight architectural decisions.
- `refactor/` — refactor workplans (existing).
- `ARCHITECTURE.md`, `HAIKU-CUE-RUNNER-SPEC.md`, `QUICKSTART.md` — top-level reference docs (existing). These will be folded into the new structure as we touch them.

## The Rule: every PR updates relevant docs

If a PR changes runtime behavior, public surface, configuration, deployment, or anything else a human needs to understand, the PR must also update the matching file(s) under `docs/`. The PR template prompts for this. A lightweight CI lint flags PRs that change source code without touching `docs/`.

**For bug fixes**: before merging, grep `docs/` for the affected symbol or behavior and confirm the docs match what's now true. If they were wrong, update them in the same PR.

## Bypass: `docs:not-applicable`

Some PRs genuinely have no docs impact — typo fixes, internal refactors with no behavior change, dependency bumps, formatting cleanups, the docs scaffolding itself. Apply the `docs:not-applicable` label to bypass the docs lint. Use it sparingly and honestly; the label is logged on every PR that carries it.

## Current state

This scaffolding is intentionally thin. Component files are one-paragraph stubs flagged `TBD`. Filling them in is the job of whoever next touches the relevant package — not a separate doc-writing effort.
