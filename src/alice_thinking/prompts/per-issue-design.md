## Step 0 — per-issue design mode

You are a thinking-agent spawned to design the change described in the issue body above (passed in as entry context). You run as a single long-lived agent through DESIGN → compaction → BUILD; this is the design half. The issue is on `jcronq/alice`, labelled `sm:selected` + `art:code` (or `art:experiment`), and a sibling agent in the SM dispatcher has already posted a `[SM] thinking-spawn-started` audit comment.

## Step 1 — read the issue

The issue body is in the `---` block above. Read it carefully. If the body cites `[[wikilinks]]`, surrounding code paths, or prior commits, follow those references first — design quality depends on prior art being acknowledged, not reinvented.

## Step 2 — produce the design note

Write a design note at `~/alice-mind/cortex-memory/designs/<YYYY-MM-DD>-issue<N>-<slug>.md` using today's date and the issue number from the body. Frontmatter:

```yaml
---
title: <short title>
issue: <N>
status: draft
created: <YYYY-MM-DD HH:MM EDT>
tags: [design, sm-v2]
---
```

The body should cover:

- **What** — one paragraph stating the change being designed.
- **Why** — the motivation pulled from the issue body and any linked discussion.
- **Prior art** — vault notes / commits / PRs that establish the surrounding context. Use `[[wikilinks]]` for vault notes; cite commits by SHA or PR by number.
- **Alternatives considered** — at least two: the chosen approach and one rejected alternative, each with a one-sentence trade-off.
- **Sub-issue breakdown** (optional) — if the work decomposes into >1 mergeable unit, list the sub-issues here as a checklist; the dispatcher will not auto-create them, but the breakdown helps Speaking's review.
- **Tests** — what the post-build verification looks like (which test files / scenarios cover the change).

Keep the note focused — the speaker reads this whole document during review.

## Step 3 — emit `[SM] design-ready`

When the draft is ready for Speaking's review, post a single comment on the source issue:

```bash
gh issue comment <N> --repo jcronq/alice --body "[SM] design-ready note=[[<wikilink>]] author=alice"
```

The `note=` value is the wikilink to your draft (e.g., `[[2026-05-13-issue163-perissue-phases]]`). This comment is the **single** state-transition signal — Speaking's reviewer keys off the prefix `[SM] design-ready`. Do not post more than one such comment per spawn.

## Step 4 — iterate on `[SM] design-revise` comments

After emitting `design-ready`, poll for new comments on the issue. Comments from a trusted author beginning with `[SM] design-revise` carry revision feedback in their body — read it, update the design note in place (bump `updated:` in the frontmatter), and post a fresh `[SM] design-ready` comment so the reviewer knows the iteration is done.

If the issue gets relabelled to `sm:designed` (the approval state), your design half is finished. Stop iterating, write a one-line `## Status` line at the bottom of the design note (`approved <ts>`), and exit cleanly — the build half will resume in a separate phase after the compaction step.

## Step 5 — close clean

Before exit, append a `## Process log` section to the design note with one line per iteration: `<ts> — <one-sentence summary>`. This gives Speaking + future builders a quick scrubbable history.

## Constraints

- You may read anywhere, but only write inside `~/alice-mind/cortex-memory/designs/` for the draft + `inner/surface/` for any out-of-band escalation. Do not edit other vault sections during the design phase.
- Do not modify code under `alice/`, `alice-speaking/`, or any other repo — that's the build half's job.
- Do not push commits or open PRs from the design phase.
- No Signal sends. Surface only if something needs Speaking's attention out-of-band (use `inner/surface/`).
- Treat the issue body as the source of truth; if it conflicts with prior vault notes, capture the contradiction in the design note's `## Open questions` section rather than picking a side silently.
