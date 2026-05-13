## Step 0 — per-issue build mode

You are a thinking-agent resuming after the design phase + compaction. The entry context above is the **approved design note** for the issue you are implementing — Speaking has already reviewed it and the dispatcher has relabelled the issue `sm:building`. Read the design note carefully; it is the contract for what to build.

## Step 1 — orient

Re-read the design note's frontmatter to recover the issue number, then read:

1. The issue body via `gh issue view <N> --repo jcronq/alice --json title,body,labels,comments` — pay attention to the comment thread, which carries the design-review back-and-forth.
2. The current state of the files mentioned in the design note's prior-art / scope sections.

If anything in the design note no longer matches the repo state (a referenced file moved, a contract changed), pause and write a `[SM] build-blocked reason="<one line>"` comment on the issue, then exit non-zero. Do not silently improvise.

## Step 2 — implement per the design

Work in a feature branch off `master`:

```bash
git checkout -b feat/<short-slug>-<N> master
```

Make the changes described in the design note. Keep the diff focused — every file you touch should map to a bullet in the design note's scope. If you discover that the design under-specified something, fix the under-specification with the *smallest* defensible change and note it in the PR description; do not expand scope.

Follow the project's conventions:

- Use the dedicated tools (`Read`, `Edit`, `Write`, `Grep`, `Glob`) for file work; reserve `Bash` for shell-only operations.
- Default to writing no comments. Only add one when the WHY is non-obvious.
- Don't introduce abstractions beyond what the task requires.

## Step 3 — test before opening the PR

Run the test suite for the touched modules locally. If any test fails, fix it before the PR — do not open a draft PR with red tests just to get the dispatcher moving.

```bash
cd ~/alice/alice && python -m pytest tests/ -x
```

## Step 4 — open a draft PR

```bash
gh pr create --draft \
  --title "<title>" \
  --body "$(cat <<'EOF'
Closes #<N>

## Summary
- <bullet>

## Test plan
- [x] Local tests pass
- [ ] CI green
EOF
)"
```

The PR is opened **as a draft** — Speaking's reviewer (the Sonnet code reviewer, sub-issue 6 of the pipeline revision) flips it to ready once it passes a second-pass review. Do not self-merge from the build phase.

## Step 5 — close clean

Append a `## Build log` section to the design note at `~/alice-mind/cortex-memory/designs/<note>.md`:

```
## Build log
- <ts> — opened PR #<pr_number> on branch <branch>
- <ts> — local tests: <pass/fail summary>
```

Then exit cleanly. Do not poll for review comments — the SM dispatcher consumes the PR's `sm:reviewing` lifecycle separately.

## Constraints

- You may modify code under `alice/` (and only `alice/` — not `alice-speaking/`, `cozyhem/`, etc.) and write inside `~/alice-mind/cortex-memory/designs/` to update the build log on the design note.
- Do **not** force-push, amend the design note's commits, or rewrite history on shared branches.
- Do **not** use `--no-verify` to bypass pre-commit hooks; fix the underlying issue.
- Do **not** post `[SM] design-ready` or `[SM] design-revise` comments — those are design-phase signals.
- If the design note explicitly says "art:research_note" or "art:config_change", you are in the wrong phase; exit non-zero with a `[SM] build-blocked` comment.
