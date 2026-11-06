"""Alice status site: Jason's status/todo page with per-item threads.

Stdlib-only (runs on a bare ``python:3.12-alpine`` image). Routes:

    GET  /, /index.html, /marked.min.js   packaged frontend (status_site/static/)
    GET  /<page>.md, /pages.json, images  content, read from STATUS_DIR (top level only)
    GET  /api/threads                     {thread_id: [message, ...]} for every thread
    GET  /api/pages                       slugs of all markdown pages (for item detail links)
    GET  /api/note?slug=<s>               leaf-item write-up from STATUS_DIR/notes/<s>.md (item modal)
    GET  /api/history?slug=<s>            {events, thread, extra} for one item (item modal timeline):
                                          events from item-history/<s>.jsonl (written by the
                                          tracker thread, see history.py), thread from
                                          threads/<s>.jsonl, extra from item-extra/<s>.jsonl
    GET  /api/item-state                  {slug: {checked, depends_on}} for every tracked item
                                          (frontend uses it to resolve ``depends_on`` across pages)
    GET  /api/chat                        {messages: [...]} — general chat log, no item context
    GET  /api/waiting-order               {order: [slug, ...]} saved drag order of "Waiting on you"
    POST /api/page-visibility             dismiss/restore/open a nav tab (shared-key auth)
    POST /api/waiting-order               save the "Waiting on you" drag order (shared-key auth)
    POST /api/reply                       Jason's reply to an item (shared-key auth):
                                          appends to the item's thread, stores attachments
                                          privately under UPLOAD_DIR, and drops a
                                          ``type: status-reply`` surface into SURFACE_DIR
                                          so Speaking gets a turn.
    POST /api/chat                        Jason's reply on the general Chat tab (shared-key auth):
                                          appends to ``.chat.jsonl`` (attachments stored under
                                          ``UPLOAD_DIR/chat/<date>/``, same rules as /api/reply) and drops a
                                          ``type: status-chat`` surface into SURFACE_DIR
                                          (no thread/item context — just the message).

Anything else is a 404: dotfiles (``.replies.jsonl``, ``.item-state.json``, ``.chat.jsonl``,
``.waiting-order.json``),
``threads/``, the key and the uploads are never served. See docs/designs/status-site.md.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from status_site.history import (
    HISTORY_DIR,
    STATE_FILE,
    Tracker,
    parse_page,
    read_history,
    read_jsonl,
    safe_slug,
)
from status_site.sources import (
    done_today as _done_today,
    known_issues as _known_issues,
    review_queue as _review_queue,
    running_now as _running_now,
    waiting_on_you as _waiting_on_you,
)
from status_site.sources.common import (
    asks_path as _asks_path,
    read_asks as _read_asks,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"index.html", "marked.min.js"}

# Content files served from STATUS_DIR: flat names only (no '/', no leading dot).
CONTENT_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(md|json|png|jpe?g|gif|webp|txt)$"
)
CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

MAX_FILES = 5
MAX_PER_FILE = 10 * 1024 * 1024
MIME_ALLOWLIST = (
    "image/*",
    "text/*",
    "application/pdf",
    "application/json",
    "application/zip",
)
MAX_TEXT = 8000
MAX_ITEM = 500
# How many chat messages /api/chat returns, newest-at-end.
MAX_CHAT_HISTORY = 500
# How many tail lines to splice into the Speaking surface for context.
CHAT_SURFACE_TAIL = 5

SPEAKING_FOOTER = (
    "For Speaking: this came from Jason via the status site ({url}, key-authenticated). "
    "Treat it like a message from Jason about this item and act on it. REPLY IN THE PAGE, "
    'not on Signal: run `status-reply {tid} "<your answer>"` (alice-tools/bin), which appends '
    "to the item's thread and shows up on his screen within seconds. Then update "
    "inner/status/index.md (move or check off the item). Use send_message only if it's "
    "urgent or needs him away from the page."
)

CHAT_SPEAKING_FOOTER = (
    "For Speaking: this came from Jason via the status site's Chat tab ({url}?p=chat, "
    "key-authenticated). No thread or item context — just a message. REPLY ON THE CHAT, "
    'not on Signal: run `status-chat "<your answer>"` (alice-tools/bin), which appends to '
    "the chat log and shows up on his screen within seconds. Use send_message only if it's "
    "urgent or needs him away from the page."
)


# ---------------------------------------------------------------- helpers


def slugify(text: str, limit: int = 60) -> str:
    """Same slug the frontend computes: lowercase, non-alnum runs -> '-', trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]


def thread_id(thread: object, item: str) -> str:
    """Thread id from the client's ``thread`` field, else derived from the item title.

    Older page versions don't send ``thread``; the title is the bold text before ':'.
    The result is always ``[a-z0-9-]`` so it is safe as a filename.
    """
    tid = re.sub(r"[^a-z0-9-]", "", str(thread or "").lower())[:60].strip("-")
    if not tid:
        tid = slugify(item.split(":")[0]) or "general"
    return tid


def safe_filename(name: object) -> str:
    """Attachment name reduced to ``[A-Za-z0-9._-]``, no leading dots, max 80 chars."""
    base = str(name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    s = re.sub(r"[^A-Za-z0-9._-]", "_", base)[-80:].lstrip(".")
    return s or "file"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass
class Config:
    status_dir: Path
    surface_dir: Path
    upload_dir: Path
    key: str
    port: int = 8095
    host: str = "0.0.0.0"
    # Upload dir as seen by Speaking (the surface lists paths she can Read).
    upload_dir_alias: str = ""
    public_url: str = "http://10.20.30.177:8095"
    max_upload: int = 25 * 1024 * 1024
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Set at boot by ``main()`` after ``make_server``. Holds the
    # background TTL-cache + pollers for the expensive overview
    # sources; the ``/index.md`` handler reads through this when
    # present and renders a "nothing yet" state when not (tests spin
    # up the server without background threads).
    pollers: "OverviewPollers | None" = field(default=None, repr=False)

    @property
    def threads_dir(self) -> Path:
        return self.status_dir / "threads"

    @property
    def reply_log(self) -> Path:
        return self.status_dir / ".replies.jsonl"

    @property
    def chat_log(self) -> Path:
        # Dot-prefixed so the static-file handler won't serve it (same guard as
        # ``.replies.jsonl``); the ``/api/chat`` endpoint is the only way out.
        return self.status_dir / ".chat.jsonl"

    @property
    def waiting_order(self) -> Path:
        # Dot-prefixed: kept out of the static handler; /api/waiting-order is the only way out.
        return self.status_dir / ".waiting-order.json"

    @property
    def max_body(self) -> int:
        # base64 inflates by 4/3; leave headroom for the JSON envelope + text.
        return self.max_upload * 4 // 3 + 50_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = dict(os.environ if env is None else env)
        key_file = e.get("KEY_FILE", "/srv/key")
        key = Path(key_file).read_text().strip()
        if not key:
            raise SystemExit(f"status_site: empty key in {key_file}")
        upload_dir = Path(e.get("UPLOAD_DIR", "/srv/uploads"))
        return cls(
            status_dir=Path(e.get("STATUS_DIR", "/srv/status")),
            surface_dir=Path(e.get("SURFACE_DIR", "/srv/surface")),
            upload_dir=upload_dir,
            key=key,
            port=int(e.get("PORT", "8095")),
            host=e.get("HOST", "0.0.0.0"),
            upload_dir_alias=e.get("UPLOAD_DIR_ALIAS", "") or str(upload_dir),
            public_url=e.get("PUBLIC_URL", cls.public_url),
            max_upload=int(e.get("MAX_UPLOAD_BYTES", str(cls.max_upload))),
        )


def read_threads(cfg: Config) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not cfg.threads_dir.is_dir():
        return out
    for f in sorted(cfg.threads_dir.glob("*.jsonl")):
        msgs = []
        for line in f.read_text(errors="replace").splitlines():
            try:
                msgs.append(json.loads(line))
            except ValueError:
                pass
        out[f.name[: -len(".jsonl")]] = msgs
    return out


def append_thread(cfg: Config, tid: str, message: dict) -> None:
    cfg.threads_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.threads_dir / f"{tid}.jsonl", "a") as f:
        f.write(json.dumps(message) + "\n")


def read_chat(cfg: Config, limit: int = MAX_CHAT_HISTORY) -> list[dict]:
    """General chat log (``.chat.jsonl``), newest-at-end, trimmed to ``limit``."""
    p = cfg.chat_log
    if not p.is_file():
        return []
    msgs: list[dict] = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            msgs.append(json.loads(line))
        except ValueError:
            pass
    return msgs[-limit:] if limit else msgs


def append_chat(cfg: Config, message: dict) -> None:
    """Append one message to the general chat log."""
    cfg.status_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.chat_log, "a") as f:
        f.write(json.dumps(message) + "\n")


class ReplyError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def mime_allowed(name: str) -> bool:
    mime = mimetypes.guess_type(name)[0]
    if not mime:
        return False
    return any(
        mime == pat or (pat.endswith("/*") and mime.startswith(pat[:-1]))
        for pat in MIME_ALLOWLIST
    )


def ingest_attachments(cfg: Config, raw_files: object, subdir: str) -> list[dict]:
    """Decode, validate and store base64 attachments under ``UPLOAD_DIR/<subdir>``.

    Every file is validated before any is written. Returns
    ``[{name, path, bytes}]`` where ``path`` is the Speaking-visible alias.
    Callers hold ``cfg.lock``. Raises ReplyError on client errors.
    """
    if raw_files is None:
        raw_files = []
    if not isinstance(raw_files, list):
        raise ReplyError(400, "bad attachment")
    if len(raw_files) > MAX_FILES:
        raise ReplyError(413, f"at most {MAX_FILES} attachments")
    cap = min(MAX_PER_FILE, cfg.max_upload)
    blobs = []
    for a in raw_files:
        if not isinstance(a, dict):
            raise ReplyError(400, "bad attachment")
        name = safe_filename(a.get("name"))
        try:
            blob = base64.b64decode(str(a.get("data", "")), validate=True)
        except (ValueError, binascii.Error):
            raise ReplyError(400, "bad attachment") from None
        if len(blob) > cap:
            raise ReplyError(413, f"{name} over {cap // (1024 * 1024)} MB")
        if not mime_allowed(name):
            raise ReplyError(415, f"{name}: file type not allowed")
        blobs.append((name, blob))

    files = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i, (name, blob) in enumerate(blobs):
        ddir = cfg.upload_dir / subdir
        ddir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = ddir / f"{stamp}-{i}-{name}"
        with open(path, "xb") as f:
            f.write(blob)
        alias = os.path.join(
            cfg.upload_dir_alias, os.path.relpath(path, cfg.upload_dir)
        )
        files.append({"name": name, "path": alias, "bytes": len(blob)})
    return files


def render_chat_surface(
    cfg: Config, *, text: str, prior: list[dict], files: list[dict] | None = None
) -> str:
    """Build the ``type: status-chat`` surface document (no thread/item fields)."""
    tail = "\n".join(f"  {m.get('from')}: {m.get('text')}" for m in prior)
    attached = "\n".join(f"  {x['path']} ({x['bytes']} bytes)" for x in files or [])
    return f"""---
priority: action
type: status-chat
reply_expected: true
created: {time.strftime("%Y-%m-%d")}
source: status-site
page: chat
---

# Jason sent a message on the Chat tab

His new message:
{text or "(no text: attachment only)"}

Attachments (Read them; stored privately, not served):
{attached or "  none"}

Recent chat (for context, last {CHAT_SURFACE_TAIL} lines before this message):
{tail or "  (none: this is the first message)"}

---
{CHAT_SPEAKING_FOOTER.format(url=cfg.public_url)}
"""


def handle_chat(cfg: Config, d: dict) -> dict:
    """Validate + persist one chat message. Raises ReplyError on client errors.

    Mirrors ``handle_reply`` but with no thread or item: the Chat
    tab is a single running log Jason uses when there's no todo item to anchor
    the conversation to.
    """
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    text = str(d.get("text", "")).strip()[:MAX_TEXT]
    raw_files = d.get("files") or []
    if not text and not raw_files:
        raise ReplyError(400, "empty message")

    with cfg.lock:
        files = ingest_attachments(cfg, raw_files, f"chat/{time.strftime('%Y-%m-%d')}")
        prior = read_chat(cfg)[-CHAT_SURFACE_TAIL:]
        append_chat(
            cfg,
            {
                "from": "jason",
                "at": now_iso(),
                "text": text,
                "files": [x["name"] for x in files],
            },
        )

        doc = render_chat_surface(cfg, text=text, prior=prior, files=files)
        cfg.surface_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{time.strftime('%Y-%m-%dT%H-%M-%S')}-status-chat.md"
        tmp = cfg.surface_dir / f".tmp-chat-{os.getpid()}-{time.time_ns()}"
        tmp.write_text(doc)
        dest = cfg.surface_dir / fname
        n = 1
        while dest.exists():
            n += 1
            dest = cfg.surface_dir / fname.replace(".md", f"-{n}.md")
        os.rename(tmp, dest)
    return {"ok": True}


def render_surface(
    cfg: Config,
    *,
    item: str,
    tid: str,
    page: str,
    text: str,
    prior: list[dict],
    files: list[dict],
) -> str:
    slug = slugify(item, 40) or "item"
    history = "\n".join(f"  {m.get('from')}: {m.get('text')}" for m in prior)
    attached = "\n".join(f"  {x['path']} ({x['bytes']} bytes)" for x in files)
    return f"""---
priority: action
type: status-reply
reply_expected: true
created: {time.strftime("%Y-%m-%d")}
detail: status-reply-{slug}
source: status-site
page: {page}
thread: {tid}
---

# Jason replied on the status page

Item: {item}
Thread: {tid}

Earlier in this thread (this item's conversation only):
{history or "  (none: this is the first message)"}

His new message:
{text or "(no text: attachment only)"}

Attachments (Read them; stored privately, not served):
{attached or "  none"}

---
{SPEAKING_FOOTER.format(url=cfg.public_url, tid=tid)}
"""


def handle_reply(cfg: Config, d: dict) -> dict:
    """Validate + persist one reply. Raises ReplyError on client errors."""
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    item = str(d.get("item", "")).strip()[:MAX_ITEM]
    tid = thread_id(d.get("thread"), item)
    text = str(d.get("text", "")).strip()[:MAX_TEXT]
    page = (
        re.sub(r"[^a-z0-9-]", "", str(d.get("page", "index")).lower())[:40] or "index"
    )

    raw_files = d.get("files") or []
    if not text and not raw_files:
        raise ReplyError(400, "empty reply")

    with cfg.lock:
        files = ingest_attachments(cfg, raw_files, tid)

        prior = read_threads(cfg).get(tid, [])[-12:]
        append_thread(
            cfg,
            tid,
            {
                "from": "jason",
                "at": now_iso(),
                "text": text,
                "files": [x["name"] for x in files],
            },
        )

        doc = render_surface(
            cfg, item=item, tid=tid, page=page, text=text, prior=prior, files=files
        )
        cfg.surface_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{time.strftime('%Y-%m-%dT%H-%M-%S')}-status-reply-{slugify(item, 40) or 'item'}.md"
        tmp = cfg.surface_dir / f".tmp-reply-{os.getpid()}-{time.time_ns()}"
        tmp.write_text(doc)
        dest = cfg.surface_dir / fname
        n = 1
        while dest.exists():
            n += 1
            dest = cfg.surface_dir / fname.replace(".md", f"-{n}.md")
        os.rename(tmp, dest)

        with open(cfg.reply_log, "a") as f:
            f.write(
                json.dumps(
                    {
                        "at": now_iso(),
                        "page": page,
                        "item": item,
                        "thread": tid,
                        "text": text,
                    }
                )
                + "\n"
            )
    return {"ok": True, "thread": tid}


# ---------------------------------------------------------------- thinking queue
#
# ``/index.md`` gets a live ``## Thinking queue`` block spliced in between
# sentinel markers. The data is a cache file (``.thinking-queue.json``) written
# every ~60s by ``alice_forge.thinking_queue_poller`` (where ``gh`` is
# authenticated); this server stays stdlib-only and never talks to GitHub.

THINKING_QUEUE_START = "<!-- thinking-queue:start -->"
THINKING_QUEUE_END = "<!-- thinking-queue:end -->"
THINKING_QUEUE_CACHE = ".thinking-queue.json"
THINKING_QUEUE_REPO = "jcronq/alice"
THINKING_QUEUE_CAP = 15
# Cache older than this is treated as unavailable (poller cadence is ~60s).
THINKING_QUEUE_STALE_SECONDS = 600
# (label, heading) in display order.
THINKING_QUEUE_PHASES = (
    ("sm:designing", "Designing"),
    ("sm:needs_study", "Needs study"),
    ("sm:design_review", "Design review"),
    ("sm:designed", "Designed"),
    ("sm:compacting", "Compacting"),
    ("sm:blocked", "Blocked"),
)


def read_thinking_queue(status_dir: Path) -> dict | None:
    """Load the poller's cache; ``None`` when missing or corrupt."""
    path = status_dir / THINKING_QUEUE_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        synced_at = float(data["synced_at"])
        items = data["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    return {
        "synced_at": synced_at,
        "items": [i for i in items if isinstance(i, dict) and "number" in i],
    }


def _format_age(updated_at: object, now: float) -> str:
    try:
        ts = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "?"
    secs = max(0, int(now - ts))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _thinking_phase(item: dict) -> int | None:
    labels = {
        (l.get("name") if isinstance(l, dict) else l) for l in item.get("labels") or []
    }
    for idx, (label, _) in enumerate(THINKING_QUEUE_PHASES):
        if label in labels:
            return idx
    return None


def render_thinking_queue(cache: dict | None, now: float | None = None) -> str:
    """Render the ``## Thinking queue`` block (without sentinel markers)."""
    now = time.time() if now is None else now
    lines = ["## Thinking queue", ""]
    if cache is None:
        lines.append("*Queue unavailable — no sync from GitHub yet.*")
        return "\n".join(lines) + "\n"
    synced_at = cache["synced_at"]
    if now - synced_at > THINKING_QUEUE_STALE_SECONDS:
        synced = datetime.fromtimestamp(synced_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"*Queue unavailable — last synced {synced}.*")
        return "\n".join(lines) + "\n"

    buckets: dict[int, list[dict]] = {}
    for item in cache["items"]:
        phase = _thinking_phase(item)
        if phase is not None:
            buckets.setdefault(phase, []).append(item)
    if not buckets:
        lines.append("*Nothing in the thinking queue.*")
        return "\n".join(lines) + "\n"

    shown = 0
    total = sum(len(v) for v in buckets.values())
    for idx, (_, heading) in enumerate(THINKING_QUEUE_PHASES):
        entries = sorted(buckets.get(idx, []), key=lambda i: int(i["number"]))
        entries = entries[: max(0, THINKING_QUEUE_CAP - shown)]
        if not entries:
            continue
        lines.append(f"**{heading}**")
        for item in entries:
            num = int(item["number"])
            title = " ".join(str(item.get("title", "")).split())
            age = _format_age(item.get("updatedAt"), now)
            lines.append(
                f"- [#{num}](https://github.com/{THINKING_QUEUE_REPO}/issues/{num})"
                f" {title} — {heading.lower()} · {age}"
            )
        shown += len(entries)
        lines.append("")
    if total > shown:
        lines.append(
            f"[{total - shown} more…](https://github.com/{THINKING_QUEUE_REPO}/issues"
            "?q=is%3Aissue+is%3Aopen+label%3Asm%3Adesigning)"
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def splice_thinking_queue(text: str, block: str) -> str:
    """Replace the region between the sentinels, or append the block once."""
    region = f"{THINKING_QUEUE_START}\n{block}{THINKING_QUEUE_END}"
    start = text.find(THINKING_QUEUE_START)
    end = text.find(THINKING_QUEUE_END, start + len(THINKING_QUEUE_START)) if start != -1 else -1
    if start != -1 and end != -1:
        return text[:start] + region + text[end + len(THINKING_QUEUE_END) :]
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + region + "\n"


def render_index_md(status_dir: Path, now: float | None = None) -> tuple[str, float] | None:
    """``index.md`` with the live thinking queue; ``None`` if the file is missing."""
    path = status_dir / "index.md"
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cache = read_thinking_queue(status_dir)
    try:
        mtime = max(mtime, (status_dir / THINKING_QUEUE_CACHE).stat().st_mtime)
    except OSError:
        pass
    return splice_thinking_queue(text, render_thinking_queue(cache, now)), mtime


# ---------------------------------------------------------------- bugs render
#
# ``/bugs.md`` is rendered on-the-fly from every ``bug-<N>.md`` detail file in
# STATUS_DIR. The server owns the Open / Fixed / Won't fix buckets — Jason
# updates a bug's state by editing the detail file's frontmatter ``status:``
# field, nothing else. See the matching intercept in ``Handler._get``.

BUG_FILE_RE = re.compile(r"^bug-(\d+)\.md$")
# Buckets rendered on ``/bugs.md``. ``in-progress`` lands between
# Open and Fixed so Jason can see dispatcher activity at a glance;
# ``dispatch-failed`` goes after Won't fix with a visual warning so
# it stands out as "needs triage, dispatcher gave up".
BUG_STATUSES = ("open", "waiting-on-review", "in-progress", "fixed", "wontfix", "dispatch-failed")
# Heading-prefix stripper for the title fallback: ``# bug-N — real title``
# becomes ``real title`` so the rendered line isn't ``bug-N — bug-N — ...``.
BUG_HEADING_PREFIX_RE = re.compile(r"^bug-\d+\s*[-–—]\s*", re.I)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the leading ``---\\n...\\n---\\n`` block.

    Returns (frontmatter_dict, body). If no frontmatter is present, returns
    an empty dict and the original text. Values are read as plain strings;
    matching surrounding single or double quotes are stripped. Lines without
    a colon or starting with ``#`` are ignored.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    front = text[4:end]
    body = text[end + len("\n---\n") :]
    data: dict[str, str] = {}
    for line in front.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key] = value
    return data, body


def parse_depends_on(value: str) -> list[str]:
    """``depends_on: [a, b]`` / ``a, b`` frontmatter value -> deduped safe slugs."""
    out: list[str] = []
    for raw in (value or "").strip().strip("[]").split(","):
        s = safe_slug(raw.strip().strip("'\""))
        if s and s not in out:
            out.append(s)
    return out


def read_item_state(status_dir: Path) -> dict[str, dict]:
    """Slim ``{slug: {checked, depends_on}}`` view of ``.item-state.json`` (tombstones dropped)."""
    try:
        d = json.loads((status_dir / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, dict):
        return {}
    return {
        slug: {"checked": it.get("checked"), "depends_on": it.get("depends_on") or []}
        for slug, it in items.items()
        if isinstance(it, dict) and not it.get("gone")
    }


def _bug_summary_fallback(reported_at: str) -> str:
    """Fallback summary line built from ``reported_at`` when missing."""
    if not reported_at:
        return ""
    try:
        dt = datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
    except ValueError:
        return f"reported {reported_at}"
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return f"reported {dt.strftime('%Y-%m-%d %H:%M UTC')}"


def _bug_title_from_heading(body: str, report_id: int) -> str:
    """Extract the first ``# `` heading from a bug detail body.

    Strips a leading ``bug-<N> —`` (or ``-``, ``–``) prefix so the rendered
    index line doesn't double up on the bug number.
    """
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            title = BUG_HEADING_PREFIX_RE.sub("", title, count=1)
            if title:
                return title
            break
    return f"bug-{report_id}"


def collect_bugs(status_dir: Path) -> tuple[dict[str, list[dict]], float]:
    """Scan ``bug-<N>.md`` files, bucket by status.

    Returns ``(buckets, max_mtime)``. ``buckets`` keys are ``open`` /
    ``fixed`` / ``wontfix`` (every key always present, possibly empty).
    Each bucket is a list of ``{"report_id": int, "title": str, "summary":
    str}`` dicts ordered newest-first by ``report_id``. ``max_mtime`` is
    the newest mtime among contributing files, or ``0.0`` if there are none.
    """
    buckets: dict[str, list[dict]] = {s: [] for s in BUG_STATUSES}
    max_mtime = 0.0
    if not status_dir.is_dir():
        return buckets, max_mtime
    for p in sorted(status_dir.glob("bug-*.md")):
        m = BUG_FILE_RE.fullmatch(p.name)
        if not m:
            continue
        try:
            text = p.read_text(errors="replace")
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
        fm, body = parse_frontmatter(text)
        try:
            report_id = int(fm.get("report_id") or m.group(1))
        except (TypeError, ValueError):
            report_id = int(m.group(1))
        status = (fm.get("status") or "").strip().lower()
        if status not in buckets:
            status = "open"
        title = (fm.get("title") or "").strip()
        if not title:
            title = _bug_title_from_heading(body, report_id)
        summary = (fm.get("summary") or "").strip()
        if not summary:
            summary = _bug_summary_fallback(fm.get("reported_at", ""))
        pr_url = (fm.get("pr_url") or "").strip()
        # Pricing fields (written by alice_bug_fix_dispatcher). Values
        # are string floats like ``0.4231``; absence means "not priced
        # yet", and ``_format_pricing_segment`` renders nothing for
        # those bugs so the Bugs page reads clean before the first
        # dispatched tick.
        estimate_usd = (fm.get("estimate_usd") or "").strip()
        actual_usd = (fm.get("actual_usd") or "").strip()
        buckets[status].append(
            {
                "report_id": report_id,
                "title": title,
                "summary": summary,
                "pr_url": pr_url,
                "estimate_usd": estimate_usd,
                "actual_usd": actual_usd,
                "depends_on": parse_depends_on(fm.get("depends_on", "")),
            }
        )
    # Promote in-progress bugs that already have a draft PR into the
    # dedicated ``waiting-on-review`` bucket. Jason wanted bug-reviews
    # to live on the Bugs pane itself (not the Overview's Ready for
    # Review section): a draft PR is the hand-off from worker back to
    # him, so it deserves its own heading above the still-cooking
    # workers.
    ready = []
    cooking = []
    for b in buckets["in-progress"]:
        (ready if b.get("pr_url") else cooking).append(b)
    buckets["waiting-on-review"] = ready
    buckets["in-progress"] = cooking
    for key in buckets:
        buckets[key].sort(key=lambda b: b["report_id"], reverse=True)
    return buckets, max_mtime


def _format_usd_local(dollars: float) -> str:
    """Status-site-local USD formatter.

    Mirrors ``alice_pricing.format_usd`` without taking a dep on the
    pricing package — ``status_site`` is stdlib-only by design (see
    the module docstring). Keep in sync.
    """
    if dollars == 0:
        return "$0.00"
    if dollars < 0:
        return "-" + _format_usd_local(-dollars)
    if dollars < 0.01:
        return f"${dollars:.4g}"
    return f"${dollars:.2f}"


def _format_pricing_segment(estimate_usd: str, actual_usd: str) -> str:
    """Build the ``$X est / $Y actual`` tail for a bug's summary line.

    * both present  → ``$X est / $Y actual``
    * only estimate → ``$X est``
    * only actual   → ``$Y actual``
    * neither       → ``""`` (caller omits the segment entirely)

    Values that don't parse as floats, or that are the sentinel
    strings ``null`` / ``none``, are treated as absent — a malformed
    frontmatter row shouldn't crash the render.
    """

    def _render(raw: str) -> str:
        s = (raw or "").strip()
        if not s or s.lower() in ("null", "none"):
            return ""
        try:
            dollars = float(s)
        except ValueError:
            return ""
        return _format_usd_local(dollars)

    est = _render(estimate_usd)
    act = _render(actual_usd)
    if est and act:
        return f"{est} est / {act} actual"
    if est:
        return f"{est} est"
    if act:
        return f"{act} actual"
    return ""


def _sum_pricing(buckets: dict[str, list[dict]]) -> dict[str, float]:
    """Return ``{"estimate": total, "actual": total}`` across every bug."""
    totals = {"estimate": 0.0, "actual": 0.0}
    for items in buckets.values():
        for b in items:
            for key, bucket in (("estimate_usd", "estimate"), ("actual_usd", "actual")):
                raw = (b.get(key) or "").strip()
                if not raw or raw.lower() in ("null", "none"):
                    continue
                try:
                    totals[bucket] += float(raw)
                except ValueError:
                    continue
    return totals


def render_bugs_md(status_dir: Path) -> tuple[str, float]:
    """Build the full ``/bugs.md`` body + newest contributing-file mtime."""
    buckets, max_mtime = collect_bugs(status_dir)
    parts = [
        "# In-game bug reports\n",
        "Every /bug report that reaches me from the game. Click one for the "
        "full state at the moment of reporting — location, flags, inventory, "
        "last 20 messages.\n",
    ]
    # Section order: Open → In progress → Fixed → Won't fix →
    # Dispatch failed (needs triage). ``prefix`` is prepended to the
    # bug title so Dispatch-failed items are visually distinct (⚠).
    sections = (
        ("Open", "open", "[ ]", ""),
        ("Waiting on review", "waiting-on-review", "[ ]", ""),
        ("In progress", "in-progress", "[ ]", ""),
        ("Fixed", "fixed", "[x]", ""),
        ("Won't fix", "wontfix", "[x]", ""),
        ("Dispatch failed (needs triage)", "dispatch-failed", "[ ]", "⚠ "),
    )
    for heading, key, box, prefix in sections:
        parts.append(f"## {heading}\n")
        items = buckets[key]
        if not items:
            parts.append("*(none yet)*\n")
            continue
        lines = []
        for b in items:
            line = f"- {box} **{prefix}bug-{b['report_id']} — {b['title']}**"
            # Build the italic summary tail: summary → [draft PR] →
            # pricing segment, joined by ``" — "``. We stitch into one
            # single italic block (``*...*``) so Jason sees one line
            # of context per bug, not a wall of separate italics.
            summary_bits: list[str] = []
            if b["summary"]:
                summary_bits.append(b["summary"])
            pr_url = b.get("pr_url", "")
            if key in ("in-progress", "waiting-on-review") and pr_url:
                summary_bits.append(f"[draft PR]({pr_url})")
            pricing_segment = _format_pricing_segment(
                b.get("estimate_usd", ""), b.get("actual_usd", "")
            )
            if pricing_segment:
                summary_bits.append(pricing_segment)
            if summary_bits:
                line += ": *" + " — ".join(summary_bits) + "*"
            if b.get("depends_on"):
                line += f" <!--deps: {', '.join(b['depends_on'])}-->"
            lines.append(line)
        parts.append("\n".join(lines) + "\n")

    # Pipeline P&L — appended at the end of the page body as a single
    # bolded line. Omitted entirely when nothing has been priced yet
    # so an all-empty bug list stays as clean as it was before.
    totals = _sum_pricing(buckets)
    if totals["estimate"] > 0 or totals["actual"] > 0:
        delta = totals["actual"] - totals["estimate"]
        parts.append(
            f"**Pipeline P&L (session):** total_estimate: {_format_usd_local(totals['estimate'])}, "
            f"total_actual: {_format_usd_local(totals['actual'])}, "
            f"delta: {_format_usd_local(delta)}\n"
        )
    return "\n".join(parts), max_mtime


# ---------------------------------------------------------------- Alice's queue
#
# Live render of unresolved surfaces in ``inner/surface/*.md``. Scanned at
# request time from the bind-mounted directory; splices into ``index.md``
# between sentinel markers so Overview shows what's pending my attention. See
# docs/designs/status-site.md and the design note under
# ``cortex-memory/designs/2026-11-05-issue621-alice-queue-live-render.md``.

# Cap of lines rendered in the Overview section. Overflow collapses into
# a single "+N more" line linking the full JSON list. Env override (rarely
# useful, but keeps the knob accessible) via ``ALICE_QUEUE_MAX``.
QUEUE_CAP = 10

# Surface filename prefix: ``YYYY-mm-ddTHH-MM-SS-<slug>.md``. Writers
# (handle_chat / handle_reply / gh-watcher) all agree on this shape.
SURFACE_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)\.md$"
)

# Markdown metacharacters escaped in user-derived slugs before they land
# in the rendered body. Covers the set that can actually damage the
# render (bold/italic/links/code); whitespace is intentionally untouched.
_MD_META_RE = re.compile(r"([\\*_\[\]()`])")

# Parses ``github-<owner>-<repo>-issue<N>`` out of a filename slug or
# ``detail:`` field (stage-b-unclassified gh-watcher notes). Owner/repo
# charset matches GitHub's; the full match is anchored on word boundaries
# so it won't eat trailing suffixes.
GITHUB_ISSUE_RE = re.compile(
    r"github-([A-Za-z0-9._-]+?)-([A-Za-z0-9._-]+?)-issue(\d+)"
)


def _queue_cap() -> int:
    """QUEUE_CAP, overridable at request time via ``ALICE_QUEUE_MAX``."""
    raw = os.environ.get("ALICE_QUEUE_MAX", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return QUEUE_CAP


def _md_escape(text: str) -> str:
    """Backslash-escape markdown metacharacters in ``text``.

    Applied to user-derived slugs (reply item slugs, stage-b note names)
    before they are rendered inline so ``*`` / ``[`` / etc. can't break
    the surrounding bold/link syntax.
    """
    return _MD_META_RE.sub(r"\\\1", text)


def format_age(seconds: float) -> str:
    """Human age: ``45s`` / ``12m`` / ``3h 4m`` / ``2d 5h``.

    Omits the zero sub-unit on the ``h`` / ``d`` forms so a clean 3-hour
    item reads ``3h`` instead of ``3h 0m``. Negative inputs (clock skew)
    are clamped to zero.
    """
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s}s"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, rem_m = divmod(m, 60)
    if h < 24:
        return f"{h}h {rem_m}m" if rem_m else f"{h}h"
    d, rem_h = divmod(h, 24)
    return f"{d}d {rem_h}h" if rem_h else f"{d}d"


def scan_surfaces(surface_dir: Path) -> list[dict]:
    """Scan flat ``*.md`` surfaces under ``surface_dir`` (oldest first).

    Resolved surfaces live under ``.handled/<date>/`` and are naturally
    excluded by the flat glob; in-flight ``.tmp-*`` writers and
    ``.gitkeep`` are dotfiles the glob also skips.

    Returns a list of dicts with: ``slug`` (filename stem minus timestamp
    prefix), ``type`` / ``source`` / ``page`` / ``thread`` / ``detail``
    (from frontmatter, empty string when absent), ``created`` (ISO local,
    second precision), ``created_epoch`` (float; used for sort + age
    math), ``path`` (Path), ``mtime`` (float).

    Per-file errors (unreadable file, parse failure) are skipped silently
    so one bad surface can't 500 the whole scan.
    """
    out: list[dict] = []
    if not surface_dir.is_dir():
        return out
    for p in surface_dir.glob("*.md"):
        # Belt-and-suspenders dotfile skip: ``Path.glob`` matched hidden
        # names on Python 3.14 even for patterns that start with ``*``,
        # so ``.tmp-reply-….md`` writers in flight would otherwise sneak
        # in. ``.handled/`` is still excluded by ``glob`` because it's a
        # directory, not a file match.
        if p.name.startswith("."):
            continue
        try:
            st = p.stat()
            text = p.read_text(errors="replace")
        except OSError:
            continue
        m = SURFACE_NAME_RE.match(p.name)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S")
            except ValueError:
                dt = datetime.fromtimestamp(st.st_mtime)
            slug = m.group(2)
        else:
            dt = datetime.fromtimestamp(st.st_mtime)
            slug = p.stem
        try:
            fm, _ = parse_frontmatter(text)
        except Exception:
            fm = {}
        out.append(
            {
                "slug": slug,
                "type": (fm.get("type") or "").strip(),
                "source": (fm.get("source") or "").strip(),
                "page": (fm.get("page") or "").strip(),
                "thread": (fm.get("thread") or "").strip(),
                "detail": (fm.get("detail") or "").strip(),
                "created": dt.isoformat(timespec="seconds"),
                "created_epoch": dt.timestamp(),
                "path": p,
                "mtime": st.st_mtime,
            }
        )
    out.sort(key=lambda it: it["created_epoch"])
    return out


def _surface_link_url(item: dict) -> str:
    """Bare URL for the surface's source, or ``""`` when nothing fits.

    Precedence: ``status-reply`` + non-empty ``page`` → page query;
    ``status-chat`` → chat query; otherwise look for a
    ``github-<owner>-<repo>-issue<N>`` marker in the filename slug or
    ``detail:`` field.
    """
    t = item.get("type", "")
    if t == "status-reply":
        page = item.get("page", "")
        if page:
            return f"?p={page}"
    elif t == "status-chat":
        return "?p=chat"
    for src in (item.get("slug", ""), item.get("detail", "")):
        if not src:
            continue
        m = GITHUB_ISSUE_RE.search(src)
        if m:
            owner, repo, n = m.group(1), m.group(2), m.group(3)
            return f"https://github.com/{owner}/{repo}/issues/{n}"
    return ""


def _surface_link_md(item: dict) -> str:
    """Markdown link for the surface, or ``""`` when there is none."""
    t = item.get("type", "")
    if t == "status-reply":
        page = item.get("page", "")
        if page:
            return f"[{page}](?p={page})"
    elif t == "status-chat":
        return "[open chat](?p=chat)"
    for src in (item.get("slug", ""), item.get("detail", "")):
        if not src:
            continue
        m = GITHUB_ISSUE_RE.search(src)
        if m:
            owner, repo, n = m.group(1), m.group(2), m.group(3)
            return f"[issue #{n}](https://github.com/{owner}/{repo}/issues/{n})"
    return ""


def _surface_label(item: dict) -> str:
    """Section label: ``chat`` / ``reply: <slug>`` / ``<type>``.

    Slugs are markdown-escaped before being spliced into the bold span.
    """
    t = item.get("type") or "unknown"
    if t == "status-chat":
        return "chat"
    if t == "status-reply":
        detail = item.get("detail", "")
        slug = detail
        if slug.startswith("status-reply-"):
            slug = slug[len("status-reply-") :]
        if not slug:
            slug = item.get("slug", "")
        return f"reply: {_md_escape(slug)}"
    return _md_escape(t)


def render_alice_queue(
    items: list[dict],
    *,
    now: float | None = None,
    cap: int | None = None,
) -> str:
    """Render the Alice's queue region body (no heading).

    Returns the italic description + bulleted list — or the one-line
    empty state when there are no items. The caller handles the
    surrounding ``## Alice's queue`` heading and the sentinel markers.
    """
    if cap is None:
        cap = _queue_cap()
    if now is None:
        now = time.time()
    if not items:
        return "*(queue clear)*"
    description = (
        "*What's pending my attention. Live from `inner/surface/` — "
        "cleared when I handle it.*"
    )
    lines: list[str] = []
    shown = items[:cap]
    for it in shown:
        age = format_age(now - float(it["created_epoch"]))
        label = _surface_label(it)
        link = _surface_link_md(it)
        line = f"- {age} — **{label}**"
        if link:
            line += f" {link}"
        lines.append(line)
    overflow = len(items) - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more — [full list](/api/surfaces)")
    return description + "\n\n" + "\n".join(lines)


SpliceSection = tuple  # (start_marker, end_marker, renderer, fallback_builder?)


def _splice_sections(body: str, sections: Iterable[SpliceSection]) -> str:
    """Splice rendered regions into ``body`` between sentinel markers.

    Each section is a tuple of ``(start_marker, end_marker, renderer)``
    with an optional fourth element ``fallback_builder(rendered: str) ->
    str`` used when the markers aren't present in the body (append-case
    composition so the deploy doesn't break before the one-time marker
    insertion lands in the mind repo).

    When both markers are present in the body, the text strictly between
    the marker lines (exclusive of the marker lines themselves) is
    replaced with ``renderer()``. The markers stay intact so the splice
    is idempotent across calls. If the markers are missing and a
    ``fallback_builder`` is given, its output is appended to the body;
    without a fallback, the renderer is skipped.

    Shape is iterable-of-tuples (not a single tuple) specifically so
    #613's Thinking queue can plug in a second entry without touching
    this helper.
    """
    out = body
    for section in sections:
        start = section[0]
        end = section[1]
        renderer = section[2]
        fallback_builder: Callable[[str], str] | None = (
            section[3] if len(section) > 3 else None
        )
        s = out.find(start)
        e = out.find(end)
        rendered = renderer()
        if s != -1 and e != -1 and e > s:
            # Keep the marker lines themselves; replace between them.
            start_eol = out.find("\n", s)
            if start_eol == -1:
                # Marker is the last thing in the body — unusual, but
                # degrade gracefully by inserting after the marker text.
                start_eol = s + len(start) - 1
            end_bol = out.rfind("\n", 0, e) + 1
            out = out[: start_eol + 1] + rendered + "\n" + out[end_bol:]
        elif fallback_builder is not None:
            tail = fallback_builder(rendered)
            out = out.rstrip("\n") + "\n" + tail.lstrip("\n")
            if not out.endswith("\n"):
                out += "\n"
    return out


ALICE_QUEUE_START = "<!-- alice-queue:start -->"
ALICE_QUEUE_END = "<!-- alice-queue:end -->"

RUNNING_NOW_START = "<!-- running-now:start -->"
RUNNING_NOW_END = "<!-- running-now:end -->"

READY_FOR_REVIEW_START = "<!-- ready-for-review:start -->"
READY_FOR_REVIEW_END = "<!-- ready-for-review:end -->"

WAITING_ON_YOU_START = "<!-- waiting-on-you:start -->"
WAITING_ON_YOU_END = "<!-- waiting-on-you:end -->"

DONE_TODAY_START = "<!-- done-today:start -->"
DONE_TODAY_END = "<!-- done-today:end -->"

KNOWN_ISSUES_START = "<!-- known-issues:start -->"
KNOWN_ISSUES_END = "<!-- known-issues:end -->"


def _agent_name() -> str:
    """Resolve the agent's name from ``mind/personae.yml``.

    Kept out of module scope + a no-literal implementation on purpose:
    ``tests/test_no_hardcoded_personae_literals.py`` forbids the
    string ``"Alice"`` appearing as a literal in ``src/`` outside its
    small allowlist. Reading the name from the config lets the heading
    ("Alice's queue") stay live without hardcoding it here.

    Fall back to the placeholder personae on any failure (missing
    file, YAML/import error, malformed data) so the fallback-append
    path still produces a reasonable heading when the mind isn't yet
    bootstrapped.
    """
    try:
        from core.config.personae import load as _load, placeholder
    except ImportError:
        return "the agent"
    mind = Path(os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind")))
    try:
        return _load(mind).agent.name
    except Exception:  # noqa: BLE001  — any failure → placeholder
        try:
            return placeholder().agent.name
        except Exception:  # noqa: BLE001
            return "the agent"


def _alice_queue_fallback(rendered: str) -> str:
    """Appended-section shape when the index.md markers are missing.

    Includes the heading + markers so the fallback is a complete
    self-contained section (the deploy stays readable while the mind
    repo edit catches up). The heading text is derived from
    :func:`_agent_name` — see that function's docstring for why.
    """
    heading = f"\n## {_agent_name()}'s queue\n"
    return (
        f"{heading}"
        f"{ALICE_QUEUE_START}\n{rendered}\n{ALICE_QUEUE_END}\n"
    )


def _running_now_fallback(rendered: str) -> str:
    return (
        "\n## Running now\n"
        f"{RUNNING_NOW_START}\n{rendered}\n{RUNNING_NOW_END}\n"
    )


def _ready_for_review_fallback(rendered: str) -> str:
    return (
        "\n## Ready for Review\n"
        f"{READY_FOR_REVIEW_START}\n{rendered}\n{READY_FOR_REVIEW_END}\n"
    )


def _waiting_on_you_fallback(rendered: str) -> str:
    return (
        "\n## Waiting on you\n"
        f"{WAITING_ON_YOU_START}\n{rendered}\n{WAITING_ON_YOU_END}\n"
    )


def _done_today_fallback(rendered: str) -> str:
    return (
        "\n## Done today\n"
        f"{DONE_TODAY_START}\n{rendered}\n{DONE_TODAY_END}\n"
    )


def _known_issues_fallback(rendered: str) -> str:
    return (
        "\n## Known issues I'm on\n"
        f"{KNOWN_ISSUES_START}\n{rendered}\n{KNOWN_ISSUES_END}\n"
    )


# ───────────────────────── Overview TTL cache ───────────────────────
#
# Expensive sources (``gh pr/issue list``, ``ps aux``, worktree scans)
# get their own background poll thread; the cache dict is read inline
# by the ``/index.md`` handler so the response never waits on the
# network. Cheap sources (hand-edit file reads, local-file globs) are
# fetched inline and skip the cache.

# Default poll intervals (seconds). Pollers for the gh-backed sources
# share the ``review_queue_renderer.poll_interval_seconds`` key (added
# by #622); the running-now source polls faster because it just reads
# local state.
DEFAULT_REVIEW_POLL_SECONDS = 300
DEFAULT_RUNNING_NOW_POLL_SECONDS = 15
DEFAULT_DONE_TODAY_POLL_SECONDS = 300


@dataclass
class _CachedValue:
    value: object
    fetched_at: float


class OverviewPollers:
    """Background threads that keep expensive section renders fresh.

    One thread per expensive source. Each thread calls its fetch
    function on ``interval`` cadence and stores the result in a dict
    protected by ``cfg.lock`` (shared with the item-history tracker
    so writers don't race against each other).

    The server's ``/index.md`` handler reads the cache without
    blocking. The first ``interval`` seconds after boot the cache is
    empty — renderers degrade to their "nothing yet" string so the
    page is still serveable.
    """

    def __init__(
        self,
        cfg: "Config",
        *,
        review_poll: float = DEFAULT_REVIEW_POLL_SECONDS,
        running_now_poll: float = DEFAULT_RUNNING_NOW_POLL_SECONDS,
        done_today_poll: float = DEFAULT_DONE_TODAY_POLL_SECONDS,
        stop: threading.Event | None = None,
    ):
        self.cfg = cfg
        self._cache: dict[str, _CachedValue] = {}
        self._review_poll = review_poll
        self._running_now_poll = running_now_poll
        self._done_today_poll = done_today_poll
        self.stop = stop or threading.Event()
        self._threads: list[threading.Thread] = []

    # ── cache accessors ─────────────────────────────────────────
    def get(self, key: str) -> object | None:
        with self.cfg.lock:
            entry = self._cache.get(key)
        return entry.value if entry else None

    def _set(self, key: str, value: object) -> None:
        with self.cfg.lock:
            self._cache[key] = _CachedValue(value=value, fetched_at=time.time())

    # ── background loops ────────────────────────────────────────
    def _loop_running_now(self) -> None:
        repo_dir = _running_now.repo_dir_from_env()
        tasks_dir = (
            Path(os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind")))
            / "inner"
            / "tasks"
        )
        while not self.stop.is_set():
            try:
                worktrees = _running_now.parse_worktrees(
                    _running_now.read_worktree_output(repo_dir)
                )
                ps_output = _running_now.read_ps_output()
                logs = _running_now.read_build_logs()
                task_records = _running_now.collect_task_records(tasks_dir)
                rows = _running_now.build_running_tasks(
                    worktrees, ps_output, logs, task_records
                )
                # Newest first by elapsed-start proxy: shortest-elapsed
                # first is more intuitive ("just started" above
                # "running for 2h"). Kept stable for the renderer.
                rows.sort(key=lambda r: r.elapsed)
                self._set("running_now", rows)
            except Exception as e:  # noqa: BLE001
                print(f"status_site: running_now poll failed: {e!r}", flush=True)
            self.stop.wait(self._running_now_poll)

    def _loop_review_queue(self) -> None:
        while not self.stop.is_set():
            try:
                design, prs, d_failed, p_failed = _review_queue.fetch()
                self._set(
                    "review_queue",
                    {
                        "design": design,
                        "pr": prs,
                        "design_failed": d_failed,
                        "pr_failed": p_failed,
                    },
                )
            except Exception as e:  # noqa: BLE001
                print(f"status_site: review_queue poll failed: {e!r}", flush=True)
            self.stop.wait(self._review_poll)

    def _loop_done_today(self) -> None:
        while not self.stop.is_set():
            try:
                repos = _done_today.repos_pr_from_config()
                today = _done_today.today_local()
                prs, failed = _done_today.fetch_merged_prs(repos, today=today)
                self._set(
                    "done_today",
                    {"prs": prs, "today": today, "failed": failed},
                )
                # Day-roll: cheap no-op when sentinel matches today.
                try:
                    _done_today.roll_day(
                        self.cfg.status_dir,
                        today=today,
                        load_prs=lambda d, repos=repos: _done_today.fetch_merged_prs(
                            repos, today=d
                        )[0],
                        load_asks=lambda d: _done_today.resolved_today(
                            _read_asks(_asks_path(self.cfg.status_dir)), d
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"status_site: done_today roll failed: {e!r}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"status_site: done_today poll failed: {e!r}", flush=True)
            self.stop.wait(self._done_today_poll)

    def start(self) -> None:
        for name, target in (
            ("running-now-poll", self._loop_running_now),
            ("review-queue-poll", self._loop_review_queue),
            ("done-today-poll", self._loop_done_today),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)


def overview_splice_sections(
    cfg: "Config",
    *,
    items: list[dict],
    now: float,
) -> list[tuple]:
    """Build the ``_splice_sections`` list for ``GET /index.md``.

    Order mirrors the Overview as it appears on the page:
    Running now → Ready for Review → Waiting on you → Alice's queue →
    Done today → Known issues I'm on. Each tuple carries sentinel
    markers, a renderer closure, and a fallback builder so the
    append-case keeps the page serveable while the mind repo catches
    up on the marker-insertion pass.
    """
    pollers = cfg.pollers
    # Running now — read cache; empty list is the "nothing yet" state.
    def _render_running_now() -> str:
        rows = (pollers.get("running_now") if pollers else None) or []
        return _running_now.render(rows)

    def _render_ready_for_review() -> str:
        cached = (pollers.get("review_queue") if pollers else None) or {}
        return _review_queue.render(
            cached.get("design", []),
            cached.get("pr", []),
            design_failed=cached.get("design_failed", []),
            pr_failed=cached.get("pr_failed", []),
        )

    def _render_waiting_on_you() -> str:
        return _waiting_on_you.render(
            cfg.status_dir / "waiting-on-you.md",
            asks_path=_asks_path(cfg.status_dir),
        )

    def _render_done_today() -> str:
        cached = (pollers.get("done_today") if pollers else None) or {}
        today = cached.get("today") or _done_today.today_local()
        asks = _read_asks(_asks_path(cfg.status_dir))
        resolved = _done_today.resolved_today(asks, today)
        return _done_today.render(cached.get("prs", []), resolved)

    def _render_known_issues() -> str:
        return _known_issues.render(cfg.status_dir / "known-issues.md")

    return [
        (
            RUNNING_NOW_START,
            RUNNING_NOW_END,
            _render_running_now,
            _running_now_fallback,
        ),
        (
            READY_FOR_REVIEW_START,
            READY_FOR_REVIEW_END,
            _render_ready_for_review,
            _ready_for_review_fallback,
        ),
        (
            WAITING_ON_YOU_START,
            WAITING_ON_YOU_END,
            _render_waiting_on_you,
            _waiting_on_you_fallback,
        ),
        (
            ALICE_QUEUE_START,
            ALICE_QUEUE_END,
            lambda: render_alice_queue(items, now=now),
            _alice_queue_fallback,
        ),
        (
            DONE_TODAY_START,
            DONE_TODAY_END,
            _render_done_today,
            _done_today_fallback,
        ),
        (
            KNOWN_ISSUES_START,
            KNOWN_ISSUES_END,
            _render_known_issues,
            _known_issues_fallback,
        ),
    ]


def surfaces_api_payload(
    items: list[dict], *, now: float | None = None
) -> dict:
    """Shape for ``GET /api/surfaces``.

    Full list, oldest first, no cap. ``link`` omitted when there is
    none. No bodies, no attachment paths — the queue is a labels-only
    view (same security model as the rendered section, which is served
    unauthenticated on the LAN).
    """
    if now is None:
        now = time.time()
    out = []
    for it in items:
        entry: dict = {
            "slug": it["slug"],
            "type": it["type"],
            "source": it["source"],
            "created": it["created"],
            "age_seconds": now - float(it["created_epoch"]),
        }
        link = _surface_link_url(it)
        if link:
            entry["link"] = link
        out.append(entry)
    return {"count": len(out), "surfaces": out}


# ---------------------------------------------------------------- done archive
#
# ``## Done today`` in index.md only makes sense for one day. On date roll,
# checked items whose completion date (from item-history) is before today move
# to ``done-archive.md`` under a ``## YYYY-MM-DD`` heading (newest first). When
# the completion date can't be determined the item is retained.

ARCHIVE_PAGE = "done-archive"
ARCHIVE_STATE = ".archive-state.json"
ARCHIVE_INTERVAL = 600.0
_DONE_HEAD_RE = re.compile(r"^##\s+done today\b", re.I)
_ANY_H12_RE = re.compile(r"^#{1,2}\s")
_TOP_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_CHECKED_ITEM_RE = re.compile(r"^[-*+]\s+\[[xX]\]\s")
_ARCHIVE_HEAD_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _split_top_items(lines: list[str]) -> list[tuple[int, int]]:
    """``[start, end)`` line ranges of each top-level list item (with its continuation)."""
    units: list[list[int]] = []
    cur: list[int] | None = None
    for i, line in enumerate(lines):
        if _TOP_ITEM_RE.match(line):
            cur = [i, i + 1]
            units.append(cur)
        elif cur is None or not line.strip():
            continue
        elif line[0] in " \t" or cur[1] == i:
            cur[1] = i + 1
        else:
            cur = None
    return [(a, b) for a, b in units]


def completed_on(status_dir: Path, slug: str, tz) -> date | None:
    """Local date the item was last checked off, or None when history can't say."""
    when = None
    for ev in read_jsonl(status_dir / HISTORY_DIR / f"{slug}.jsonl"):
        t = ev.get("type")
        if t == "checked" or (t == "appeared" and ev.get("checked") is True):
            when = ev.get("at")
        elif t == "unchecked":
            when = None
    if not when:
        return None
    try:
        dt = datetime.fromisoformat(str(when))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    return dt.date()


def _merge_archive(text: str, additions: dict[str, list[list[str]]]) -> str:
    """Insert item line-groups under their ``## <date>`` headings, newest-first."""
    lines = text.split("\n") if text.strip() else ["# Done archive"]
    preamble: list[str] = []
    sections: list[list] = []  # [date, body_lines]
    for line in lines:
        m = _ARCHIVE_HEAD_RE.match(line)
        if m:
            sections.append([m.group(1), []])
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    for d in sorted(additions, reverse=True):
        sec = next((s for s in sections if s[0] == d), None)
        if sec is None:
            sec = [d, []]
            at = next((i for i, s in enumerate(sections) if s[0] < d), len(sections))
            sections.insert(at, sec)
        existing = {ln for ln in sec[1] if ln.strip()}
        for group in additions[d]:
            if group[0] in existing:
                continue
            sec[1] = [*sec[1], *group]
    parts = ["\n".join(preamble).strip("\n")] if "".join(preamble).strip() else []
    for d, body in sections:
        chunk = f"## {d}"
        body_txt = "\n".join(body).strip("\n")
        parts.append(chunk + ("\n\n" + body_txt if body_txt else ""))
    return "\n\n".join(parts) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def archive_done(cfg: Config, now: datetime | None = None) -> list[dict]:
    """Move stale checked items out of index.md's ``## Done today`` into done-archive.md.

    Returns ``[{"slug", "date", "lines"}, ...]`` for each archived item.
    """
    now = now or _local_now()
    today = now.date()
    index_path = cfg.status_dir / "index.md"
    archive_path = cfg.status_dir / f"{ARCHIVE_PAGE}.md"
    with cfg.lock:
        try:
            lines = index_path.read_text().split("\n")
        except OSError:
            return []
        start = next((i for i, ln in enumerate(lines) if _DONE_HEAD_RE.match(ln)), None)
        if start is None:
            return []
        end = next(
            (i for i in range(start + 1, len(lines)) if _ANY_H12_RE.match(lines[i])),
            len(lines),
        )
        body = lines[start + 1 : end]
        archived: list[dict] = []
        drop: set[int] = set()
        for a, b in _split_top_items(body):
            if not _CHECKED_ITEM_RE.match(body[a]):
                continue
            parsed = parse_page("\n".join(body[a:b]), "index")
            if not parsed:
                continue
            slug = parsed[0].slug
            day = completed_on(cfg.status_dir, slug, now.tzinfo)
            if day is None or day >= today:
                continue
            archived.append({"slug": slug, "date": day.isoformat(), "lines": body[a:b]})
            drop.update(range(start + 1 + a, start + 1 + b))
        if not archived:
            return []
        additions: dict[str, list[list[str]]] = {}
        for item in archived:
            additions.setdefault(item["date"], []).append(item["lines"])
        try:
            old = archive_path.read_text()
        except OSError:
            old = ""
        # Archive first: a crash between the two writes duplicates, never loses.
        _atomic_write(archive_path, _merge_archive(old, additions))
        _atomic_write(index_path, "\n".join(l for i, l in enumerate(lines) if i not in drop))
    return archived


def archive_tick(cfg: Config, now: datetime | None = None) -> list[dict] | None:
    """Run ``archive_done`` once per local date; None if today already ran."""
    now = now or _local_now()
    today = now.date().isoformat()
    state_path = cfg.status_dir / ARCHIVE_STATE
    try:
        last = json.loads(state_path.read_text()).get("last_run")
    except (OSError, ValueError, AttributeError):
        last = None
    if last == today:
        return None
    archived = archive_done(cfg, now)
    _atomic_write(state_path, json.dumps({"last_run": today, "archived": len(archived)}))
    return archived


def run_archive_roller(
    cfg: Config,
    interval: float = ARCHIVE_INTERVAL,
    stop: threading.Event | None = None,
    clock=_local_now,
) -> None:
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            archived = archive_tick(cfg, clock())
            if archived:
                print(f"status_site: archived {len(archived)} done item(s)", flush=True)
        except Exception as e:  # never let a bad page kill the thread
            print(f"status_site: done-archive failed: {e!r}", flush=True)
        stop.wait(interval)


def start_archive_roller(cfg: Config, interval: float = ARCHIVE_INTERVAL) -> threading.Thread:
    t = threading.Thread(
        target=run_archive_roller, args=(cfg, interval), name="done-archive", daemon=True
    )
    t.start()
    return t


# ---------------------------------------------------------------- HTTP


def read_note(cfg: Config, slug: str) -> dict | None:
    """Leaf-item write-up: STATUS_DIR/notes/<slug>.md, rendered in the item modal."""
    slug = re.sub(r"[^a-z0-9-]", "", str(slug).lower())[:60]
    if not slug:
        return None
    p = cfg.status_dir / "notes" / f"{slug}.md"
    if not p.is_file():
        return None
    return {
        "slug": slug,
        "markdown": p.read_text()[:200_000],
        "updated": p.stat().st_mtime,
    }


def list_pages(cfg: Config) -> list[str]:
    """Slugs of every top-level markdown page (detail pages included)."""
    return sorted(
        p.stem for p in cfg.status_dir.glob("*.md") if not p.name.startswith(".")
    )


def handle_page_visibility(cfg: Config, d: dict) -> dict:
    """Dismiss or restore a nav tab: sets/clears "hidden" on its pages.json entry."""
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    slug = re.sub(r"[^a-z0-9-]", "", str(d.get("slug", "")).lower())[:40]
    action = str(d.get("action", "dismiss"))
    if not slug or slug == "index":
        raise ReplyError(400, "that tab can't be dismissed")
    if action not in ("dismiss", "restore", "open"):
        raise ReplyError(400, "bad action")
    path = cfg.status_dir / "pages.json"
    with cfg.lock:
        try:
            pages = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ReplyError(500, "pages.json unreadable") from None
        hit = False
        for p in pages:
            if isinstance(p, dict) and p.get("slug") == slug:
                hit = True
                if action == "dismiss":
                    p["hidden"] = True
                else:
                    p.pop("hidden", None)
        if not hit and action == "open":
            md = cfg.status_dir / f"{slug}.md"
            if not md.is_file():
                raise ReplyError(404, "no such page")
            title = slug.replace("-", " ").capitalize()
            for line in md.read_text().splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()[:60]
                    break
            pages.append({"slug": slug, "title": title})
            hit = True
        elif hit and action == "open":
            for p in pages:
                if isinstance(p, dict) and p.get("slug") == slug:
                    p.pop("hidden", None)
        if not hit:
            raise ReplyError(404, "no such tab")
        tmp = path.with_name(".pages.json.tmp")
        tmp.write_text(json.dumps(pages, indent=1) + "\n")
        os.replace(tmp, path)
    return {"ok": True, "slug": slug, "hidden": action == "dismiss", "pages": pages}


WAITING_ORDER_MAX = 500
WAITING_SLUG_RE = re.compile(r"[a-z0-9-]{1,60}")


def read_waiting_order(cfg: Config) -> list[str]:
    try:
        data = json.loads(cfg.waiting_order.read_text())
    except (OSError, ValueError):
        return []
    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, list):
        return []
    return [s for s in order if isinstance(s, str) and WAITING_SLUG_RE.fullmatch(s)]


def handle_waiting_order(cfg: Config, d: dict) -> dict:
    """Persist the drag-sorted slug sequence for the "Waiting on you" list."""
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    order = d.get("order")
    if not isinstance(order, list) or len(order) > WAITING_ORDER_MAX:
        raise ReplyError(400, "bad order")
    clean: list[str] = []
    for s in order:
        if not isinstance(s, str) or not WAITING_SLUG_RE.fullmatch(s):
            raise ReplyError(400, "bad slug in order")
        if s not in clean:
            clean.append(s)
    path = cfg.waiting_order
    with cfg.lock:
        tmp = path.with_name(".waiting-order.json.tmp")
        tmp.write_text(json.dumps({"order": clean}) + "\n")
        os.replace(tmp, path)
    return {"ok": True, "order": clean}


class Handler(BaseHTTPRequestHandler):
    cfg: Config  # set by make_server
    server_version = "StatusSite/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (stdlib signature)
        # Never log query strings: Jason's link carries ?k=<key>.
        path = self.path.split("?", 1)[0] if isinstance(self.path, str) else "-"
        print(
            f"{self.address_string()} {self.command} {path} {args[1] if len(args) > 1 else ''}",
            flush=True,
        )

    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str,
        mtime: float | None = None,
        head: bool = False,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if mtime is not None:
            self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, code: int, obj: object, head: bool = False) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", head=head)

    def _resolve(self, path: str) -> Path | None:
        name = unquote(path).lstrip("/") or "index.html"
        if name in STATIC_FILES:
            return STATIC_DIR / name
        if CONTENT_NAME_RE.fullmatch(name):
            p = self.cfg.status_dir / name
            if p.is_file():
                return p
        return None

    def _get(self, head: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/api/note":
            q = parse_qs(urlsplit(self.path).query)
            note = read_note(self.cfg, (q.get("slug") or [""])[0])
            if note is None:
                return self._json(404, {"error": "no note"}, head=head)
            return self._json(200, note, head=head)
        if path == "/api/history":
            q = parse_qs(urlsplit(self.path).query)
            hist = read_history(self.cfg.status_dir, (q.get("slug") or [""])[0])
            if hist is None:
                return self._json(400, {"error": "bad slug"}, head=head)
            return self._json(200, hist, head=head)
        if path == "/api/pages":
            return self._json(200, list_pages(self.cfg), head=head)
        if path == "/api/threads":
            return self._json(200, read_threads(self.cfg), head=head)
        if path == "/api/waiting-order":
            return self._json(
                200, {"order": read_waiting_order(self.cfg)}, head=head
            )
        if path == "/api/item-state":
            return self._json(200, read_item_state(self.cfg.status_dir), head=head)
        if path == "/api/chat":
            return self._json(
                200, {"messages": read_chat(self.cfg)}, head=head
            )
        if path == "/bugs.md":
            body, mtime = render_bugs_md(self.cfg.status_dir)
            return self._send(
                200,
                body.encode("utf-8"),
                "text/markdown; charset=utf-8",
                mtime=mtime if mtime > 0 else None,
                head=head,
            )
        if path == "/api/surfaces":
            items = scan_surfaces(self.cfg.surface_dir)
            return self._json(
                200, surfaces_api_payload(items, now=time.time()), head=head
            )
        if path == "/index.md":
            index_path = self.cfg.status_dir / "index.md"
            if index_path.is_file():
                try:
                    source = index_path.read_text(errors="replace")
                    file_mtime = index_path.stat().st_mtime
                except OSError:
                    return self._json(404, {"error": "not found"}, head=head)
                items = scan_surfaces(self.cfg.surface_dir)
                newest = max(
                    (float(it["mtime"]) for it in items), default=0.0
                )
                now = time.time()
                sections = overview_splice_sections(
                    self.cfg, items=items, now=now
                )
                rendered = _splice_sections(source, sections)
                cache = read_thinking_queue(self.cfg.status_dir)
                rendered = splice_thinking_queue(
                    rendered, render_thinking_queue(cache, now)
                )
                try:
                    thinking_mtime = (
                        self.cfg.status_dir / THINKING_QUEUE_CACHE
                    ).stat().st_mtime
                except OSError:
                    thinking_mtime = 0.0
                mtime = max(file_mtime, newest, thinking_mtime)
                return self._send(
                    200,
                    rendered.encode("utf-8"),
                    "text/markdown; charset=utf-8",
                    mtime=mtime if mtime > 0 else None,
                    head=head,
                )
        p = self._resolve(path)
        if p is None:
            return self._json(404, {"error": "not found"}, head=head)
        try:
            body = p.read_bytes()
            mtime = p.stat().st_mtime
        except OSError:
            return self._json(404, {"error": "not found"}, head=head)
        ctype = (
            CONTENT_TYPES.get(p.suffix)
            or mimetypes.guess_type(p.name)[0]
            or "application/octet-stream"
        )
        if p.suffix in (".html", ".js"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype, mtime=mtime, head=head)

    def do_GET(self) -> None:
        self._get(head=False)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        handlers = {
            "/api/reply": handle_reply,
            "/api/page-visibility": handle_page_visibility,
            "/api/chat": handle_chat,
            "/api/waiting-order": handle_waiting_order,
        }
        fn = handlers.get(route)
        if fn is None:
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "bad length"})
        if n > self.cfg.max_body:
            self.close_connection = True
            return self._json(413, {"error": "too long"})
        if n <= 0:
            return self._json(400, {"error": "bad json"})
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            return self._json(400, {"error": "bad json"})
        try:
            return self._json(200, fn(self.cfg, d))
        except ReplyError as e:
            return self._json(e.code, {"error": e.msg})


def make_server(cfg: Config) -> ThreadingHTTPServer:
    cfg.threads_dir.mkdir(parents=True, exist_ok=True)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg})
    srv = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    cfg = Config.from_env()
    srv = make_server(cfg)
    if os.environ.get("ITEM_HISTORY", "1") != "0":
        Tracker(cfg.status_dir, cfg.lock).start(
            float(os.environ.get("ITEM_HISTORY_INTERVAL", "10"))
        )
    if os.environ.get("OVERVIEW_POLLERS", "1") != "0":
        pollers = OverviewPollers(
            cfg,
            review_poll=float(
                os.environ.get(
                    "REVIEW_POLL_SECONDS", str(DEFAULT_REVIEW_POLL_SECONDS)
                )
            ),
            running_now_poll=float(
                os.environ.get(
                    "RUNNING_NOW_POLL_SECONDS",
                    str(DEFAULT_RUNNING_NOW_POLL_SECONDS),
                )
            ),
            done_today_poll=float(
                os.environ.get(
                    "DONE_TODAY_POLL_SECONDS",
                    str(DEFAULT_DONE_TODAY_POLL_SECONDS),
                )
            ),
        )
        cfg.pollers = pollers
        pollers.start()
    if os.environ.get("ARCHIVE_ON_ROLL", "1") != "0":
        start_archive_roller(
            cfg, float(os.environ.get("ARCHIVE_INTERVAL", str(ARCHIVE_INTERVAL)))
        )
    print(f"status_site: serving {cfg.status_dir} on {cfg.host}:{cfg.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
