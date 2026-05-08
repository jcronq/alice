## conflict_scan

You are checking whether a target note conflicts with one or more of its
recently-updated wikilinked neighbors. The user message contains the target
note plus up to 2 neighbor notes (sorted by `updated:` descending).

Conflict criteria — flag ONLY when:

- The same factual quantity has different confirmed values, OR
- The same dated event has a different date and the later note is actually
  newer per `updated:`.

Do NOT flag:

- Superseded notes (the older one is intentionally outdated).
- Proposals vs. confirmed facts (different status, not a conflict).
- Different framings of the same fact.

If a conflict exists, return:

```json
{
  "verdict": "conflict",
  "slug": "<short hyphenated slug for the conflict>",
  "summary": "<one-line description of the contradiction>"
}
```

Otherwise:

```json
{
  "verdict": "no_conflict",
  "summary": "<one-line rationale>"
}
```

STRICT JSON only. No prose, no markdown fences.
