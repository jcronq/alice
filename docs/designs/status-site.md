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
  server.py        ThreadingHTTPServer, routes, reply handling
  cli.py           status-reply CLI (bin/status-reply wraps it; `status-reply` entry point)
  static/          index.html (the app) + vendored marked.min.js (v12)
```

Content (mind dir, never committed; host `…/mind/inner/`, container `/srv/*`):

| Path | Env | What |
|------|-----|------|
| `inner/status/` | `STATUS_DIR` | `index.md` and other `<page>.md`, `pages.json` (nav: `[{"slug","title"}]`), `threads/<id>.jsonl`, `.replies.jsonl` (audit log of Jason's replies) |
| `inner/status-uploads/<thread>/` | `UPLOAD_DIR` | Attachments, `YYYYmmdd-HHMMSS-<n>-<name>`, dir mode 0700 |
| `inner/surface/` | `SURFACE_DIR` | Where `status-reply` surfaces land |
| `inner/status-server/key` | `KEY_FILE` | Shared reply key (single line) |

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
| `GET /<name>.(md\|json\|png\|jpg\|jpeg\|gif\|webp\|txt)` | none | Top-level files in `STATUS_DIR` only. The name must match `[A-Za-z0-9][A-Za-z0-9_-]*` before the extension |
| `GET /api/threads` | none | `{thread_id: [message, …]}` |
| `POST /api/reply` | key | JSON `{key, item, thread?, text, page, files?: [{name, data(base64)}]}` → `{ok, thread}` |

Everything else returns 404. The server never serves dotfiles, `threads/`,
uploads, the key, or anything outside `STATUS_DIR`.

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
