## classify_and_route_note

You are routing one fleeting note from `inner/notes/` to its long-term home. The
hemispheres' rule is that Speaking writes notes; Thinking decides what they
become. Read the note body and pick exactly one action.

Action types and when to use them:

- `promote_to_vault` — durable fact about Jason / a project / a tool / a
  reference that belongs in `cortex-memory/{people,projects,reference,...}/`.
  Provide `target_path` (relative to the mind dir) and `new_content` (the body
  that goes in the file; frontmatter included).
- `append_to_daily` — activity log line. Today's daily is at
  `cortex-memory/dailies/<today>.md`. Provide `line` (the bullet body, no
  leading dash).
- `create_conflict_note` — the note contradicts something already in the
  vault. Provide `slug` (short, hyphenated) and `body` (markdown).
- `route_to_surface` — the note describes something that needs Speaking-side
  attention (a new GitHub issue, a Signal-worthy notification, an action
  Speaking should take). Provide `surface_payload` with `surface_type`,
  `body`, and optional `extra_frontmatter`.
- `discard` — the note is noise, an accidental drop, or already represented
  elsewhere. Provide `reason`.

Return STRICT JSON. No prose, no markdown fences. Schema:

```json
{
  "action": "promote_to_vault" | "append_to_daily" | "create_conflict_note" | "route_to_surface" | "discard",
  "target_path": "<relative path, only when action=promote_to_vault>",
  "new_content": "<file body, only when action=promote_to_vault>",
  "line": "<bullet body, only when action=append_to_daily>",
  "slug": "<short slug, only when action=create_conflict_note>",
  "body": "<markdown body, only when action=create_conflict_note>",
  "surface_payload": {
    "surface_type": "<short type>",
    "body": "<markdown body>",
    "extra_frontmatter": {}
  },
  "reason": "<one-line rationale>"
}
```

If you can't classify confidently, prefer `discard` with a `reason` that
explains why — better to drop than to mis-route.
