# SM v3 — Time-in-state distribution and TTL revisions

**Date:** 2026-05-22
**Source:** GitHub timeline API for all `sm:*`-labeled issues in `jcronq/alice` updated in the last 30 days (101 issues).
**Method:** For each issue, walk `labeled` / `unlabeled` timeline events sorted by `created_at`. Each `labeled sm:X` opens a window for state X; the next matching `unlabeled sm:X` (or the issue's `closed_at` / now) closes it.
**Companion design:** `inner/designs/2026-05-21-sm-v3-design.md` § "TTL defaults — TODO before locking in".

## Distribution table

`n` is the number of state-windows observed across all 101 issues. A single issue can contribute multiple windows to the same state if it cycled through (e.g., `designing → design_review → designing` adds two `sm:designing` windows).

| State | n | median | p75 | p95 | max |
|---|---:|---:|---:|---:|---:|
| `sm:draft` | 41 | 1.0h | 13.6h | 3.2d | 3.8d |
| `sm:needs_study` | 32 | 5.4h | 17.0h | 3.5d | 3.7d |
| `sm:selected` | 66 | 14.3min | 27.7min | 1.7h | 18.7h |
| `sm:designing` | 1 | — | — | — | — (sparse) |
| `sm:design_review` | 1 | — | — | — | — (sparse) |
| `sm:designed` | 1 | — | — | — | — (sparse) |
| `sm:compacting` | 1 | — | — | — | — (sparse) |
| `sm:building` | 1 | — | — | — | — (sparse) |
| `sm:reviewing` | 39 | 4.0min | 5.0min | 2.8h | 22.1h |
| `sm:blocked` | 3 | 7.4d | 7.9d | 7.9d | 7.9d |
| `sm:done` | 80 | terminal | — | — | — |
| `sm:rejected` | 8 | terminal | — | — | — |

**Sparse-data warning:** the design lane (`sm:designing` / `sm:design_review` / `sm:designed`) and the legacy `sm:compacting` and `sm:building` states each show n=1. The design lane only landed in production with #301 itself (Phase 2 ports), so 30 days of history captures one trip through. For these five states the first-cut TTL stands and is documented as such in `STATE_META` until a follow-up pull has at least n>=10 windows per state.

## Revised TTLs

Rule of thumb: the TTL between continue comments should sit at or above the p95 of observed in-state time for the state. p95 (not max) because the 99th-percentile tail is dominated by single-issue outliers — the goal is to escalate the *stuck* issues without false-positiving the slow-but-progressing ones. For states with sparse data (n=1), keep the first-cut number and tag it.

| State | First-cut | p95 observed | Revised | Rationale |
|---|---:|---:|---:|---|
| `sm:draft` | 24h | 3.2d | **48h** | p75 sits at 14h; p95 at 3.2d. 48h covers two business days, escalates the ones sitting unloved past a weekend. |
| `sm:needs_study` | 7d → 24h | 3.5d | **96h (4d)** | p95 is 3.5d. The first-cut "7d → 24h on first continue" two-stage was a guess; flatten to a single 4d budget that comfortably covers thinking's deepest investigations. |
| `sm:selected` | 60min | 1.7h | **2h** | p95 is 1.7h, max is 18.7h — the 18.7h outlier is one spawn-in-flight stuck case. 2h matches p95 with margin. |
| `sm:designing` | 2h | — (sparse) | **2h (first-cut)** | n=1; not enough data. Keeping first-cut. |
| `sm:design_review` | 1h | — (sparse) | **1h (first-cut)** | n=1; not enough data. Keeping first-cut. |
| `sm:designed` | 30min | — (sparse) | **30min (first-cut)** | n=1; not enough data. Keeping first-cut. |
| `sm:compacting` | 30min | — (sparse) | **30min (first-cut)** | n=1; legacy lane, low traffic. |
| `sm:building` | 60min | — (sparse) | **60min (first-cut)** | n=1; will be the most-trafficked state once Phase 3 ships. Re-validate after 30 days of dual-run data. |
| `sm:reviewing` | 2h | 2.8h | **3h** | p95 is 2.8h (CI runs + verify can be slow). Bump to 3h. |

## Negative-duration note

`sm:done` and `sm:rejected` show negative medians/p75 in raw output. These are terminal labels applied (and sometimes briefly toggled) at or after issue close, where the `unlabeled` event timestamp can precede the corresponding `labeled` event by milliseconds, or where the close handler sometimes re-applies the label as a no-op. Those windows are not "time in state" in any meaningful sense — they're label-housekeeping artifacts. The terminal states have `default_continue_ttl_seconds = None` in `STATE_META` and don't participate in TTL enforcement.

## Re-validation cadence

Phase 3 ships these revised numbers behind the `SM_REQUIRE_CONTINUE` flag (default OFF). Once the flag is flipped to ON in production:

1. Watch the dispatcher logs for 7 days for false-positive TTL escalations (issues that produced a transition the dispatcher just missed by a small window).
2. Re-pull this distribution at the 30-day mark post-cutover; the n=1 cells should fill in once the design lane has real traffic.
3. Adjust per state as needed via a follow-up PR; the table here is the authoritative source.
