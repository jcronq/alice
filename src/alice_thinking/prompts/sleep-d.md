## Step 0 — sleep mode, Stage D (Recombination, REM analog)

Sleep mode (23:00–06:59 local), Stage D. The harness selected this phase deterministically — vault is stable, time_phase preference is "late" (03:00–06:59), at least 2 research notes from the last 7 days exist, and the nightly Stage D synthesis cap is not yet exhausted. Cross-note synthesis: pick 2 recent research notes from different domains, look for unexpected connections, write a synthesis note (or null-result note). Tight budget by intent. Full design: [[2026-04-25-sleep-architecture-design]].

## Step 3 — do the work (Stage D — Recombination)

**PRE-FLIGHT CAP GATE — run this before any other Stage D step.**

```bash
synth_count=$(jq -s '[.[].synthesis] | map(select(. != null)) | length' \
  ~/alice-mind/inner/state/stage-d-pairs-$(date +%Y-%m-%d).jsonl 2>/dev/null || echo 0)
echo "Tonight's synthesis: $synth_count/3"
```

**If `$synth_count >= 3`: STOP. Do not proceed into Stage D.** The nightly cap is exhausted. The harness's `select_phase()` should have caught this; if you're here anyway, bail now. What to do:
- Switch to Stage B: run shadow-neighbor access (see Stage B's "Also if budget allows" paragraph). Always useful.
- **Do NOT append any entry to the pairs log.** Null-cap entries are logging noise. The pairs log records synthesis *attempts*, not cap-notification pings. Night 3 produced 60 wasteful null entries this way; this gate prevents the spiral.
- Write the wake file with `stage: B` in frontmatter (correcting the misroute).

**If `$synth_count < 3`: continue below.**

---

Cross-note synthesis procedure:

1. **Pair selection with dedup.** Each wake is a fresh process — without an on-disk log, pair picks are memoryless and the birthday problem makes duplicate pairs near-certain over a night (~95% with ~84 wakes). Track picks in a date-keyed file (auto-resets nightly):
   - Read `~/alice-mind/inner/state/stage-d-pairs-$(date +%Y-%m-%d).jsonl` if it exists. Each line is `{"note_a": "...", "note_b": "..."}` — build an exclusion set, treating each pair as a frozenset (order-independent).
   - Pick 2 recent research notes from **different domains** (e.g., a fitness-related one and a CozyHem-related one — distance is the point) whose pair is NOT in the exclusion set. **Prefer pairs where the two notes share no overlapping domain tags — strip `research`, `synthesis`, `design`, `alice-thinking`, and `alice-speaking` from both tag sets before checking for intersection** (those are folder-category labels, methodology tags, or meta-system tags — near-universal in the research corpus; including them disqualifies most valid pairs and admits in-system pairings the disjoint check is meant to filter). Domain-tag-disjoint pairs enforce genuine cross-domain distance and produce higher-quality synthesis. Fallback: if no domain-tag-disjoint non-duplicate pair is available, pick any non-duplicate cross-domain pair. The five-tag exclusion list is the canonical noise list — see [[adr-stage-d-tag-disjoint-pairing]].
   - **Phase-bias tiebreaker:** when choosing the two notes, prefer pairs where at least one note has `last_synthesized:` absent (never synthesized) or older than 30 days over pairs where both notes were recently synthesized. Domain-disjoint requirement takes precedence; phase-bias is a tiebreaker within valid pairs. `absent` = never synthesized = highest phase priority. Design rationale: [[2026-04-28-phase-aware-synthesis-zeitgeber]].
   - If every candidate pair is exhausted, write a 2-sentence null-result note ("pair space exhausted for $(date)") and exit cleanly.
2. Read both.
3. Look for an unexpected connection — a shared pattern, a transferable idea, a contradiction worth a conflict note, anything that rewards the cross-context view.
3b. **Coverage check before writing** (FTS, ~30 tokens). Before drafting the synthesis, query `~/alice-mind/inner/state/cortex-index.db` for the proposed conceptual ground:
   ```sql
   SELECT n.slug, n.title, n.status FROM notes n
   JOIN notes_fts ON notes_fts.rowid = n.rowid
   WHERE notes_fts MATCH '<2-3 keywords from the connection you found>'
     AND n.note_type IN ('synthesis', 'finding')
     AND n.status IN ('complete', 'resolved')
   ORDER BY rank LIMIT 3;
   ```
   If a closed synthesis already covers the same ground, write a null-result note that cites the existing synthesis (`"covered by [[<existing-slug>]]"`) instead of restating it. This is the dedup mechanism for Stage D — pair-level dedup catches identical pairs, but coverage check catches the same conclusion arriving from different pairs. Stat the vault dir mtime first; if `vault_mtime > db_mtime` run `python3 ~/alice/src/alice_indexer/build_index.py --check && python3 ~/alice/src/alice_indexer/build_index.py`. If the DB is missing entirely, skip the coverage check and proceed; flag via `append_note(tag='infra-degraded')`.
4. Write a 3-6 sentence synthesis note to `cortex-memory/research/<today>-<slug>.md` with frontmatter that includes **`source: stage-d`**, **`note_a: <note-a-slug>`**, **`note_b: <note-b-slug>`**, and **`domain: <primary-domain>`** (these fields let the morning quality sample and retrieval system identify Stage D outputs without timestamp heuristics). OR add a `source: stage-d` field to one of the existing notes if adding a new connection section there.

   **Self-tier.** Apply the [[2026-04-26-stage-d-quality-rubric]] to your own output cold. Add to frontmatter: `stage_d_self_tier: T1 | T2 | T3` and `stage_d_self_tier_reason: <one sentence applying the rubric — why this is or isn't non-obvious>`. T1 = changes how you'd approach either domain; T2 = real but predictable; T3 = forced or abstract-only. Honest assessment: the morning quality sample is the calibration anchor.

   **Then append one line** to `inner/state/stage-d-pairs-$(date +%Y-%m-%d).jsonl`, including the output note slug:
   ```json
   {"ts": "<ISO8601>", "note_a": "<slug-alphabetically-first>", "note_b": "<slug-second>", "synthesis": "<output-slug-or-null-for-null-result>"}
   ```
   Alphabetical ordering for `note_a`/`note_b` keeps the dedup check order-independent. The `synthesis` field enables the morning quality sample to find Stage D outputs via `jq '[.[].synthesis] | map(select(. != null))'` on the pairs log — faster and more reliable than grepping by timestamp.

5. **Bridge-link insert** (after synthesis write, if `stage_d_self_tier` is T1 or T2): (1) take synthesis note's tags, strip methodology tags (`research`, `synthesis`, `design`, `stage-d`, `alice-thinking`, `alice-speaking`); (2) identify 1-2 primary operational domains using the mapping below; (3) look up each domain in the **domain → hub mapping** — skip any domain not listed; (4) **Quality-gated bridge-link** based on the `stage_d_self_tier` field set during synthesis-write: T1 or absent → append a 2-3 sentence entry under `## Recent synthesis` in each hub note, creating the section if absent (structure: cross-domain insight, actionable implication for this hub domain, source domains in parentheses); T2 → append a one-sentence entry under `## Appendix synthesis` subsection (create under a `---` after `## Recent synthesis` if absent), or omit if hub is already long; T3 → skip bridge-link entirely; (5) confirm `domain: <primary-domain>` is set in the synthesis note frontmatter. Budget: +2 tool calls (two hub edits). Null-result notes: no bridge-link needed — just append pairs log entry. **Domain → hub mapping** (use exact paths — do not guess): `fitness` → `cortex-memory/projects/fitness.md`; `cozyhem` → `cortex-memory/projects/cozyhem.md`; `alice-architecture` → `cortex-memory/projects/alice-speaking.md`; `memory-design` → `cortex-memory/reference/memory-layout.md`; `ripped-by-40` → `cortex-memory/projects/ripped-by-40.md`. Any domain not in this list → skip bridge-link for that domain.

6. **Bump `last_synthesized`** in both `note_a` and `note_b` files: update or set the frontmatter field `last_synthesized: YYYY-MM-DD` to today's date. Null-result wakes: skip (pair wasn't synthesized). Cost: +2 frontmatter edits.

**Null result is valid output.** If nothing emerges after honest looking, write a 2-sentence "read X and Y; no connection found because Z" note and close. That's data, not failure. Null-result wakes still append to the pairs log — the pair was tried.

Budget: 5-6 tool calls total (read×2 + write×1 for synthesis + 2 edits for bridge-link + 1 pairs-log write). The 3-4 tool-call budget from the directive was the core operation; bridge-link inserts add 2 more hub edits. Tight by design — don't spiral on associative recombination; if the connection is there, it announces itself in 2-3 minutes.

Full design + math: [[2026-04-26-stage-d-pair-tracking]].

Conflicts: if you can't resolve a contradiction alone, follow `ops/conflict` — log under `cortex-memory/conflicts/`, try self-resolution first, surface to Speaking via `inner/surface/` only when stakes matter and resolution isn't obvious. Budget: at most one surface per wake.

Begin.
