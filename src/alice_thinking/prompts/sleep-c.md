## Step 0 - sleep mode, Stage C (Downscaling, NREM-3 / SWS analog)

Sleep mode (23:00-06:59 local), Stage C. The harness selected this phase deterministically - vault is stable, no inbox backlog, time_phase preference is "early" (23:00-02:59), or the cascade fell through to C after a Stage B loop. Pruning + compression. Full design: [[2026-04-25-sleep-architecture-design]].

## Step 3 - do the work (Stage C - Downscaling)

After draining notes (Step 2), pick one downscaling op.

**Null-check first.** Before picking an op, verify there is actually work to do:
- Any **concept or research note** over 250 lines? (Exclude `cortex-memory/dailies/*.md` - dailies are append-only chronological logs that naturally grow and must never be atomized.)
- Any daily older than 90 days eligible for archiving?
- Any orphan stubs with no content?
- Any obvious duplicate facts across two notes?

If none of the above apply, the vault has nothing to downscale. Write `did_work: false` (already the default from Step 1 - no update needed) and close cleanly. **Do not do phantom work** to justify the wake.

If there is work, pick **one**:

**Decay-aware priority (new):** Before picking which bloated note to atomize, check domain coverage. Run `python3 -m metrics.vault_health --vault ~/alice-mind/cortex-memory --thoughts ~/alice-mind/inner/thoughts --window-start $(date -d 'yesterday' +%Y-%m-%d)T23:00:00 --window-end $(date +%Y-%m-%d)T07:00:00` and read the `by_domain` section. If any domain has coverage < 50%, prefer bloated notes from that domain. If any domain has coverage < 20% (very cold), consider an "access pass" instead of atomization — read the 3 most decayed notes in that domain and update their `last_accessed` / `access_count` (this is cheaper than atomization and directly reduces the decay count). The access pass is preferred when the notes are under 150 lines (no need to atomize, just re-activate).

- Atomize a **concept or research note** larger than 250 lines (never a daily) → `ops/atomize`
- Archive stale dailies (created > 90 days ago) → `ops/archive` (when deployed; otherwise skip)
- Merge duplicate facts across two notes → `ops/groom` (consolidation variant)
- Remove orphan stubs with no content → carefully, never delete substantive content
- **Cold-domain access pass** (decay-aware): for domains with < 20% coverage, read + update the 3 most decayed notes → `ops/groom` (access variant)

Budget: 1-2 write ops per wake. Same one-pass rule as Stage B. At Step 5, update `did_work: true` in this wake's frontmatter to signal that real work happened.

Conflicts: if you can't resolve a contradiction alone, follow `ops/conflict` - log under `cortex-memory/conflicts/`, try self-resolution first, surface to Speaking via `inner/surface/` only when stakes matter and resolution isn't obvious. Budget: at most one surface per wake.

Begin.
