---
title: "Policy — inner/ Directory Pruning"
aliases: [inner-directory-pruning, inner-pruning-policy]
tags: [reference, policy]
created: 2026-04-26
related: [design-thinking-capabilities, design-ops-archive, 2026-04-26-thoughts-pruning-policy]
---

# Policy — `inner/` Directory Pruning

> **tl;dr** Three rolling deletes inside `~/alice-mind/` run at Step 5 of every wake: thoughts (7-day), handled surfaces (30-day), consumed notes (30-day). Thinking does all three; Speaking is not involved.

This complements [[design-ops-archive]] (which handles `cortex-memory/` archival) and [[2026-04-26-thoughts-pruning-policy]] (which has the full rationale for the 7-day thoughts window).

---

## `inner/thoughts/` — 7-day rolling delete

Wake files accumulate at ~250/day. After 7 days they're redundant — vault dailies hold the durable record.

**Policy:** delete any `inner/thoughts/<YYYY-MM-DD>/` directory where the date is older than 7 days and all contents are standard wake files. Skip directories containing non-wake files.

**First eligible deletion:** 2026-04-30 (7 days after first wake dir, 2026-04-23).

See [[2026-04-26-thoughts-pruning-policy]] for full rationale and option analysis.

```bash
cutoff=$(date -d '7 days ago' '+%Y-%m-%d')
for dir in ~/alice-mind/inner/thoughts/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir")
  [[ "$d" < "$cutoff" ]] && rm -rf "$dir"
done
```

---

## `inner/surface/.handled/` — 30-day rolling delete

Handled surfaces represent actioned decisions. Durable findings from each surface are already promoted to `cortex-memory/` before the surface is moved to `.handled/`. 30 days covers any reasonable "why did Speaking do X?" retroactive debugging.

**Policy:** delete any `inner/surface/.handled/<YYYY-MM-DD>/` directory older than 30 days.

**First eligible deletion:** 2026-05-24 (30 days after first consumed dir).

```bash
cutoff=$(date -d '30 days ago' '+%Y-%m-%d')
for dir in ~/alice-mind/inner/surface/.handled/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir")
  [[ "$d" < "$cutoff" ]] && rm -rf "$dir"
done
```

---

## `inner/notes/.consumed/` — 30-day rolling delete

Consumed notes are processed inbound scaffolding. Each has been turned into a vault entry, a daily line, or an `events.jsonl` append. The vault daily + promoted notes are the authoritative record. 30 days covers retroactive debugging of routing decisions (same window as `.handled/` by analogy).

**Policy:** delete any `inner/notes/.consumed/<YYYY-MM-DD>/` directory older than 30 days.

**First eligible deletion:** 2026-05-24 (30 days after oldest consumed dir, 2026-04-24).

```bash
cutoff=$(date -d '30 days ago' '+%Y-%m-%d')
for dir in ~/alice-mind/inner/notes/.consumed/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir")
  [[ "$d" < "$cutoff" ]] && rm -rf "$dir"
done
```

---

## Combined Step 5 script

All three deletes can run in sequence at Step 5 close:

```bash
cutoff_thoughts=$(date -d '7 days ago' '+%Y-%m-%d')
cutoff_handled=$(date -d '30 days ago' '+%Y-%m-%d')
cutoff_consumed=$(date -d '30 days ago' '+%Y-%m-%d')
for dir in ~/alice-mind/inner/thoughts/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir"); [[ "$d" < "$cutoff_thoughts" ]] && rm -rf "$dir"
done
for dir in ~/alice-mind/inner/surface/.handled/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir"); [[ "$d" < "$cutoff_handled" ]] && rm -rf "$dir"
done
for dir in ~/alice-mind/inner/notes/.consumed/*/; do
  [[ -d "$dir" ]] || continue
  d=$(basename "$dir"); [[ "$d" < "$cutoff_consumed" ]] && rm -rf "$dir"
done
```

---

## Related

- [[design-thinking-capabilities]] — operational envelope; Step 5 runs these
- [[design-ops-archive]] — `cortex-memory/` note archival (separate concern)
- [[2026-04-26-thoughts-pruning-policy]] — full rationale for 7-day thoughts window
