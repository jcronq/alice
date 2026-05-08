## produce_grooming_diff

You are grooming one vault note. The user message contains the current file
contents and a short vault index summary (resolved aliases + slug map). Your
job: produce a typed `Diff` that fixes any of the following without rewriting
unrelated content:

- Frontmatter drift (missing `updated`, missing or stale `access_count`,
  missing `created`, key order, etc.).
- Broken wikilinks (the index summary tells you which targets resolve).
- Stale or contradictory section content (only when the note is internally
  inconsistent — leave subjective claims alone).

Return STRICT JSON. Schema:

```json
{
  "frontmatter_changes": [
    {"key": "<key>", "new_value": "<value or null>"}
  ],
  "wikilink_fixes": [
    {"old_target": "<target as it appears in [[...]]>", "new_target": "<corrected target>"}
  ],
  "section_edits": [
    {"heading": "<heading text>", "new_body": "<replacement body, no heading line>"}
  ],
  "rationale": "<one-line summary of what changed>"
}
```

If nothing needs to change, return all-empty arrays and `rationale: "no
changes needed"`.

Do NOT rewrite the body wholesale. Do NOT add or remove sections. Wikilink
fixes apply globally. Section edits replace exactly the body under the
named heading. Frontmatter changes preserve all other keys.
