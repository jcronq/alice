## stale_finding_lint

You are checking whether a single research note's stated problem has been
resolved by newer vault state. The user message contains the candidate note
and a one-paragraph snippet from up to 3 newer-touched neighbor notes.

If the note's problem is clearly resolved (the newer notes describe a fix,
a measurement, or a decision that closes the open question), return:

```json
{
  "verdict": "resolved",
  "summary": "<one-line description of what resolved it>"
}
```

Otherwise:

```json
{
  "verdict": "still_open",
  "summary": "<one-line description of why it's still open>"
}
```

STRICT JSON only. No prose, no markdown fences.
