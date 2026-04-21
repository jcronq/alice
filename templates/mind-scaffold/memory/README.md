# memory/

Alice's memory lives here. Three tiers documented in `../CLAUDE.md`:

- `YYYY-MM-DD.md` — daily narrative logs
- `events.jsonl` — structured event stream (schema: `EVENTS-SCHEMA.md`)
- Claude Code auto-memory — long-term curated memories (loaded from
  `.claude/projects/.../memory/` by the runtime)

Add your own subdirectories for domain-specific notes (e.g. `work/`,
`health/`, `projects/`, `people/`). Alice will discover them when CLAUDE.md
or a skill points at them.
