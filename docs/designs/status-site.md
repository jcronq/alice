# Status site

Code: `src/status_site/` · Deploy: `status-site` service in `sandbox/docker-compose.yml`
(container `alice-status`, port 8095) · Bridge: `tools/claude-code-bridge/`

## Purpose

Jason's live status/todo page. Alice keeps markdown checklists in the mind dir
(`inner/status/*.md`) and the site renders them as GFM pages with a nav. Every
open checklist item, and every item under a "Waiting on you" heading, gets a
collapsible chat thread. When Jason types into a thread (text and/or attachments),
the server:

1. appends his message to that item's thread,
2. stores any attachments privately, and
3. drops a `type: status-reply` **surface** into `inner/surface/`, which gives
   Speaking a turn.

Speaking answers **in the page** with `status-reply <thread> "<text>"`, which
appends to the same thread. The page polls threads every 5 s, so the answer
shows up on his screen within seconds. Signal is only for urgent replies. The
page reloads its markdown every 60 s, and it reloads itself when the served
`index.html` changes (`Last-Modified` on a `HEAD`), so frontend deploys reach open
tabs on their own.

Other producers write pages too: status generators on dev hosts publish
`<page>.md` over a restricted ssh key (see `tools/claude-code-bridge/README.md`).

## Data layout

Code (repo, read-only in the container):

```
src/status_site/
  server.py                   ThreadingHTTPServer, routes, reply handling,
                              overview-splice site + background TTL pollers
  cli.py                      status-reply / status-chat / waiting-ask CLIs
                              (bin/* wrappers point here)
  sources/                    per-section renderers (pure functions, cached
                              upstream when the source is expensive):
    common.py                 staleness footer + asks.jsonl reader
    running_now.py            git worktree + ps + build-log tail
    review_queue.py           in-process adapter around
                              alice_forge.review_queue_renderer
    waiting_on_you.py         hand-zone + structured asks hybrid
    done_today.py             gh merged-PR search + resolved-asks + day roll
    known_issues.py           hand-zone pass-through
  static/                     index.html (app) + vendored marked.min.js (v12)
```

Content (mind dir, never committed; host `…/mind/inner/`, container `/srv/*`):

| Path | Env | What |
|------|-----|------|
| `inner/status/` | `STATUS_DIR` | `index.md` (splice container; sections rendered from ground truth — see below) and other `<page>.md`, `pages.json` (nav: `[{"slug","title"}]`), `threads/<id>.jsonl`, `.replies.jsonl` (audit log), `asks.jsonl` (structured waiting-on-you asks, one record per line), `waiting-on-you.md` + `known-issues.md` (hand-edit zones spliced into the Overview), `done-archive.md` (day-roll archive) |
| `inner/status-uploads/<thread>/` | `UPLOAD_DIR` | Attachments, `YYYYmmdd-HHMMSS-<n>-<name>`, dir mode 0700 |
| `inner/surface/` | `SURFACE_DIR` | Where `status-reply` / `status-chat` surfaces land. Also scanned at request time for the **Alice's queue** section on `/index.md` — flat `*.md` files are the "pending my attention" list; resolved surfaces move to `.handled/<date>/` and drop out naturally. The server splices the rendered list into `index.md` between `<!-- alice-queue:start -->` / `<!-- alice-queue:end -->` sentinels; if the markers aren't present the section is appended to the served body (nothing is written to disk). |
| `inner/status-server/key` | `KEY_FILE` | Shared reply key (single line) |
| `inner/state/` | `ALICE_STATE_DB_PATH` | `alice-state.db` (+ `-wal`/`-shm` WAL sidecars) — read-only access for the Overview DB-backed pipeline/review-queue/done-today readers (#814); written by the memory worker inside the `alice` container, never by status-site |

Other env: `PORT` (8095), `HOST` (0.0.0.0), `UPLOAD_DIR_ALIAS` (the upload dir as
Speaking sees it, used in surface paths), `PUBLIC_URL`, `MAX_UPLOAD_BYTES` (25 MB).

Thread line: `{"from": "jason"|"alice", "at": ISO8601, "text": str, "files"?: [name]}`.

**Thread id**: the item's bold title, slugified (`[a-z0-9-]`, ≤60). The frontend
computes it and sends it as `thread`. If `thread` is missing (older page
versions), the server uses the text before the first `:` of `item`, then
`general`.

## HTTP surface

| Route | Auth | Notes |
|-------|------|-------|
| `GET /`, `/index.html`, `/marked.min.js` | none | Packaged static assets |
| `GET /index.md` | none | Live render: the on-disk `index.md` is a splice container — six sections are spliced in between sentinel marker comments on every request. Five are auto-rendered from ground truth (**Running now**, **Ready for Review**, **Waiting on you**'s structured-ask sub-list, **Alice's queue**, **Done today**) and two are hand-edit zones (`waiting-on-you.md`'s prose, `known-issues.md`). Expensive sources (`gh pr/issue list`, `git worktree`, `ps aux`) are kept warm in a background TTL cache so the request path never blocks on the network. `Last-Modified = max(index.md mtime, newest surface mtime)`. See the section-source table below. |
| `GET /<name>.(md\|json\|png\|jpg\|jpeg\|gif\|webp\|txt)` | none | Top-level files in `STATUS_DIR` only. The name must match `[A-Za-z0-9][A-Za-z0-9_-]*` before the extension |
| `GET /api/threads` | none | `{thread_id: [message, …]}` |
| `GET /api/surfaces` | none | `{"count": N, "surfaces": [{slug, type, source, created, age_seconds, link?}, …]}` — full unresolved-surface list oldest-first, no cap, no bodies, no attachment paths. Backs the "full list" link that the Alice's queue section shows on overflow. |
| `GET /api/reviews` | none | Slugs of `notes/*.md` whose frontmatter has `kind: decide` or `needs: review`; drives the "Review plan →" button on *Waiting on you* items |
| `GET /api/review?slug=` | none | One review item `{slug, title, repo, issue, url, markdown}`; rendered at `?p=review-<slug>` |
| `POST /api/review-action` | key | JSON `{key, slug, action: approve\|modify\|reject, notes?}` → drops a `type: status-review-<action>` surface; Speaking does the GitHub side (the server never calls `gh`). `modify` requires notes |
| `POST /api/reply` | key | JSON `{key, item, thread?, text, page, files?: [{name, data(base64)}]}` → `{ok, thread}` |

Everything else returns 404. The server never serves dotfiles, `threads/`,
uploads, the key, or anything outside `STATUS_DIR`.

## Overview splice sections

`GET /index.md` runs every section renderer in `src/status_site/sources/`
and splices each one between a pair of HTML-comment sentinels in the
served response. The on-disk `index.md` is a container — Alice edits
only the headings + sentinel pairs there; the content between them is
replaced on every request. Missing-sentinel sections fall through to
an append path so the deploy stays readable until the mind repo lands
the marker pairs. See
`cortex-memory/designs/2026-11-05-issue638-overview-auto-render.md` for
the decision log.

| Section | Markers | Source | Cache |
|---------|---------|--------|-------|
| Running now | `<!-- running-now:{start,end} -->` | `git worktree list --porcelain` ∪ `ps aux` grep ∪ `/tmp/alice-build-*/build-*.log` tails ∪ `inner/tasks/*.md` | 15s background poll (`RUNNING_NOW_POLL_SECONDS`) |
| Ready for Review | `<!-- ready-for-review:{start,end} -->` | `gh issue list --label sm:design_review` + `gh pr list --json ...reviewDecision`. Repos from `alice.config.json.review_queue_renderer.repos_pr` / `repos_design_review` | 5min background poll (`REVIEW_POLL_SECONDS`) |
| Waiting on you | `<!-- waiting-on-you:{start,end} -->` | Hand-zone: `inner/status/waiting-on-you.md` verbatim. Structured: `inner/status/asks.jsonl` unresolved records rendered oldest-first as `- [ ] <text>` | Inline (cheap file reads) |
| Alice's queue | `<!-- alice-queue:{start,end} -->` | `inner/surface/*.md` scan (same as pre-#638) | Inline |
| Done today | `<!-- done-today:{start,end} -->` | `gh pr list --search "is:merged merged:>=<today>"` across watched repos + `asks.jsonl` records with `resolved_at` on today-local | 5min background poll (`DONE_TODAY_POLL_SECONDS`) |
| Known issues I'm on | `<!-- known-issues:{start,end} -->` | `inner/status/known-issues.md` verbatim | Inline |

Hand-edit zones DO NOT carry a staleness footer. Every other
renderer's output ends with::

    *last_rendered: <iso-local> · source: <name>*

The frontend (`static/index.html`) scans every page `<em>` for that
shape and adds a `.stale` class to the preceding `<h2>` when the
delta exceeds 30 s, red-tinting the heading. Hand zones are not
polled so they never go stale.

### `asks.jsonl`

One JSON object per line, append-only (plus the atomic rewrite on
resolve). Shape:

```json
{"id":"abcd1234","text":"decide SM v2 cutover","created":"2026-11-05T12:34:56-04:00","resolved":false}
```

Fields:

- `id` — short uuid4 prefix (8 hex chars), printed by the
  `waiting-ask` CLI on append.
- `text` — the ask verbatim.
- `created` / `resolved_at` — ISO8601 with offset. `resolved_at`
  appears only on resolved records.
- `resolved` — boolean. Resolved records drop off Waiting-on-you
  and (when `resolved_at` lands on today's local date) appear under
  Done today's "Asks resolved today" sub-list.

### `waiting-ask` CLI

Entry point `src/status_site/cli.py:main_waiting_ask`, wrapper
`bin/waiting-ask`. Three modes:

- `waiting-ask "<text>"` — append a new pending ask, print its id.
- `waiting-ask --resolve <id>` — mark an ask resolved. Idempotent on
  already-resolved; exits 1 when the id doesn't exist.
- `waiting-ask --list` — print unresolved asks, oldest first.

Writes land in `$STATUS_DIR/asks.jsonl` (`$ALICE_MIND/inner/status/`
by default).

### Day-roll archive

The Done-today background poller also calls `roll_day()` on every
tick. Semantics: once per local-day boundary the previous day's
rendered content is appended to `$STATUS_DIR/done-archive.md`, with a
sentinel file (`.done-archive-last-roll`) guarding against
double-append on restart.

## Security model

- **LAN-only.** Port 8095 is published on aimax1's LAN address with no TLS
  and no tunnel. Reads are unauthenticated: anyone on the LAN can read the
  pages and threads, so nothing secret goes on a page.
- **Shared key for writes.** `POST /api/reply` requires the key
  (`hmac.compare_digest`). Wrong or missing key → 403, and nothing is written.
  Jason's link carries `?k=<key>` once. The page stores it in `localStorage` and
  strips it from the URL, and the server never logs query strings. To rotate:
  write a new `key` and send a new link (the container reads the key at
  startup, so recreate it).
- **Private uploads.** Attachments go to `UPLOAD_DIR`, which is outside
  `STATUS_DIR` and has no route. Speaking reads them from the mind dir via
  the paths in the surface.
- **Limits.** Request body ≤ 25 MB × 4/3 + 50 KB (413 before reading). Each
  decoded attachment ≤ 25 MB, ≤5 attachments, text ≤8000 chars, item ≤500.
- **Input neutralization.** Thread id `[a-z0-9-]`, page `[a-z0-9-]`, and
  attachment names (basename only, `[A-Za-z0-9._-]`, no leading dot) are
  reduced to safe charsets before they touch the filesystem or surface
  frontmatter.
- **Container.** Runs as uid 1000 with the code mounted read-only and only the
  four mind dirs mounted. It is separate from the `alice` container, so it can be
  restarted without touching Speaking.

## Surface contract

File `inner/surface/<YYYY-mm-ddTHH-MM-SS>-status-reply-<item-slug>.md`. It is
written to a dotfile first and then renamed atomically. A `-N` suffix is added
if the name already exists.

```markdown
---
priority: action
type: status-reply
reply_expected: true
created: 2026-10-27
detail: status-reply-<item-slug≤40>
source: status-site
page: <page>
thread: <thread-id>
---

# Jason replied on the status page

Item: <item text, ≤500>
Thread: <thread-id>

Earlier in this thread (this item's conversation only):
  jason: …          (last 12 messages before this one, or "(none: this is the first message)")
  alice: …

His new message:
<text, or "(no text: attachment only)">

Attachments (Read them; stored privately, not served):
  /home/alice/alice-mind/inner/status-uploads/<thread>/<file> (<bytes> bytes)   (or "none")

---
For Speaking: … REPLY IN THE PAGE … run `status-reply <thread-id> "<your answer>"` …
```

What Speaking does with it: act on the message, reply with `status-reply`, and
update the page markdown (check off or move the item). It uses `send_message`
only if the reply is urgent.

## Operations

```bash
# deploy / pick up code changes (only this container)
ssh cronqj@aimax1 'cd ~/dev/alice/runtime/sandbox && ALICE_ROOT=.. . ../bin/_alice-env.sh \
  && docker compose up -d --no-deps --force-recreate status-site'
# tests
uv run pytest tests/test_status_site.py
```
