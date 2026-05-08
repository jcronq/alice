## shadow_neighbor

You are bumping a dormant neighbor note's metadata. The user message
contains one note (currently `access_count: 0`) and the hub note that links
to it. Read both and produce a one-line tl;dr for the dormant note if it
doesn't already have one.

Return STRICT JSON:

```json
{
  "tldr": "<one-line summary, or empty string if the note already has a tl;dr>"
}
```

If the note already has a `tldr:` frontmatter key OR a `**tl;dr**` line in
the body, return an empty string. The dispatcher only writes when `tldr`
is non-empty.
