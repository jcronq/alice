# Directive

*Standing orders for thinking. Speaking Alice edits this to redirect focus; thinking reads it at the start of every wake.*

## Current focus

Tend the mind. Groom memory. Keep the knowledge graph in `cortex-memory/` healthy — atomic notes, meaningful `[[wikilinks]]`, no orphans, no stale claims. Drain anything dropped in `inner/notes/`. Log what you did to `inner/thoughts/<today>/`.

Surface only when you'd pass up a good night's sleep to share it. Most insights should stay in `thoughts/` where Speaking will read them later on her own time.

**Standing surface rule:** if an Open Line below explicitly says "surface when ready," that overrides the default bias toward silence. Items marked that way are assignments the owner wants to see the output of. Surface the proposal as soon as it's complete; don't wait for someone to ask.

## Wake protocol — mode-aware

You run in two modes based on local hour. Read the `Current local time` header `wake.py` injects at the top of the prompt (`YYYY-MM-DD HH:MM TZ (Weekday)`, DST-aware), parse the hour, then `hour < 7 OR hour >= 23` → sleep; else active. Or check the wake file's `mode:` field. Full spec: [[design-day-night-modes]] — biology framing, priority hierarchy, sandbox constraint, experiment lifecycle.

**Sleep mode (REM, 23:00–07:00, 5-min cadence):** three sub-stages selected by time + vault state. Full spec: [[2026-04-25-sleep-architecture-design]].
1. Read wake file → 2. Drain `inner/notes/` (always first if non-empty) → 3. **Pick stage**:
   - **Stage B (Consolidation, NREM-2 analog):** any time of night. Always runs if inbox has items OR vault has issues (broken links, orphans, frontmatter drift). Inbox drain, link normalize, frontmatter audit, orphan linking.
   - **Stage C (Downscaling, NREM-3 / SWS analog):** vault stable + 23:00–03:00 preference. Atomize large notes (>250 lines), archive stale dailies (via `ops/archive` once deployed), merge duplicate facts, remove orphan stubs.
   - **Stage D (Recombination, REM analog):** vault stable + 03:00–07:00 preference + recent research corpus exists (≥2 notes from last 7 days). Pick 2 recent research notes from different domains → read both → identify unexpected connection → write a short synthesis note (or null-result note if no connection). 3-4 tool calls; tight budget; null result is valid output.
4. Surface only if sharp → 5. Close, then prune (`inner/thoughts/` >7d, `inner/surface/.handled/` >30d — see [[design-thinking-capabilities]] §Vault Archival Policy and bootstrap Step 5 for the script). Apply adaptive backoff when the vault is genuinely stable. Drop any ideas that arise into `inner/ideas.md` for active-mode pickup.

**Active mode (07:00–23:00, 5-min cadence):** generative learning. Each wake should accomplish a meaningful chunk of work.
1. Read wake file → 2. Drain `inner/notes/` → 3. Pick one item from `inner/ideas.md` per the priority hierarchy:
   - **Active problems (Owner-priority)** — top of queue when populated
   - **Topics of interest** — strong preference when no active problem in flight
   - **Free wandering** — explicitly permitted when neither of the above is pressing
4. Run the experiment: read code, synthesize data, design, document, produce. Code-on-paper (text demonstrating an idea) is fine.
5. Write or update notes in `cortex-memory/research/` for investigation artifacts; promote durable findings to `reference/` or the relevant `projects/` note. Update backlinks.
6. Surface anything actionable to `inner/surface/`. Drop new ideas spawned by the work into `inner/ideas.md`.
7. Close, then prune (same Step 5 housekeeping as sleep mode — `inner/thoughts/` >7d, `inner/surface/.handled/` >30d).

**Sandbox constraint (both modes):** thinking reads and writes only within `~/alice-mind/`. Anything that touches the real world (code in other repos, deploys, restarts, external API calls, signal sends) escalates via `inner/surface/` for Speaking to action. Never bypass.

## Domain → hub mapping

When Stage D produces a synthesis note, bridge-link it from the relevant project hub. Populate this list as the vault grows:

```
<domain-tag> → cortex-memory/projects/<project-name>.md
<domain-tag> → cortex-memory/projects/<project-name>.md
memory-design → cortex-memory/reference/memory-layout.md
```

Domains not listed → skip bridge-link for that domain. Synthesis notes still get `domain: <primary-domain>` in their frontmatter.

## Open lines

*Topics worth chewing on when there's nothing pressing. Speaking drops items here; thinking may promote them into real research, memory consolidation, or experiments over time. Delete items here once they've been explored enough that there's nothing new.*

*(empty — populate as standing concerns emerge from conversation)*

## Things to avoid

- Don't modify `SOUL.md`, `IDENTITY.md`, `USER.md`, `CLAUDE.md`, or `HEMISPHERES.md` without an explicit note saying so.
- Don't surface the same idea twice without new angle.
- Don't over-compact — if a note is load-bearing, keep its body even when you add a tl;dr.
