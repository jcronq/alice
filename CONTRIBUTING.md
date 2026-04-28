# Contributing

Thanks for your interest in alice. **The project is not currently accepting
external contributions.**

## After cloning — wire the pre-push secret scanner

This repo ships a pre-push hook at `.githooks/pre-push` that scans pushes
for known secret patterns (Anthropic / GitHub / Slack / AWS keys, etc.).
Git won't auto-enable hooks from a tracked path (security feature), so
each clone has to opt in:

```bash
git config core.hooksPath .githooks
```

Run it once after cloning. It's cheap insurance against pasting a key
into a commit by accident.

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
