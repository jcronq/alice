# Skills

Deterministic procedures Alice follows for recurring workflows. Think of
them as pre-commit scripts for the assistant — "when X happens, do exactly
Y, not 'some judgment call about Y'".

Skills exist because LLM reasoning is cheap to invoke but expensive to
audit. A skill:
- Codifies a procedure once so it doesn't get re-derived (differently)
  every session
- Gives you something concrete to point at when a workflow breaks
- Makes it trivial to update behavior: edit the SKILL.md, not the prompt

## The one starter

- `log-journal/` — example skill showing the pattern

## Adding a new skill

When a recurring task happens 3+ times, make it a skill:

```
.claude/skills/<name>/SKILL.md
```

With frontmatter:

```yaml
---
name: <skill-id>
description: Use when <concrete trigger>. Does <one-line summary of effect>.
---
```

Then the body: steps, don'ts, edge cases. Short and specific beats long
and comprehensive.

## Invocation

Claude Code auto-loads skills from this directory when running with
`cwd=/home/alice/alice-mind` (the default inside Alice's sandbox). The
`description` field is what triggers invocation — keep it specific so
skills don't fire on unrelated prompts.
