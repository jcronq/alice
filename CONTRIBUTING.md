# Contributing

Thanks for your interest in alice. **The project is not currently accepting
external contributions.**

## After cloning — wire the hooks

This repo ships two tracked git hooks under `.githooks/`:

- `pre-commit` — lints staged markdown files for valid YAML frontmatter
  (see `scripts/lint_markdown.py`). Catches the kind of bug that landed
  in alice-mind on 2026-05-20 (unquoted multi-word value with embedded
  colons → unparseable frontmatter → broken render on GitHub).
- `pre-push` — scans pushes for known secret patterns (Anthropic / GitHub
  / Slack / AWS keys, etc.).

Git won't auto-enable hooks from a tracked path (security feature), so
each clone has to opt in:

```bash
./scripts/install-hooks.sh
```

That runs `git config core.hooksPath .githooks` and makes the hooks
executable. Idempotent — safe to re-run.

The markdown lint is best-effort layered: PyYAML's `safe_load` is the
must-have check; `markdownlint-cli2` is invoked if installed (`npm i -g
markdownlint-cli2`), otherwise skipped with a one-line note.

## What helps right now

- **Bug reports** — open a GitHub issue with reproduction steps.
- **Feature ideas** — open an issue describing what you'd want and why.
  No promises on what gets picked up.

## What doesn't help right now

- Unsolicited pull requests. They will likely be closed without review.
  If a discussion in an issue concludes that a PR is welcome, you'll be
  asked to sign the [CLA](CLA.md) before it can be merged.

## Why a CLA?

The [CLA](CLA.md) lets the project be relicensed in the future (for
example, dual-licensed under a commercial license alongside MIT) without
having to track down every past contributor for permission. Standard
practice for solo-maintained projects that may want to add commercial
options later.
