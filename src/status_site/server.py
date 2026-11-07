"""Alice status site: Jason's status/todo page with per-item threads.

Stdlib-only (runs on a bare ``python:3.12-alpine`` image). Routes:

    GET  /, /index.html, /marked.min.js, /chat-ux.js   packaged frontend (status_site/static/)
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
    GET  /api/reviews                     slugs of review-shaped notes (``kind: decide`` or
                                          ``needs: review`` frontmatter) — drives the "Review
                                          plan →" button on *Waiting on you* items
    GET  /api/review?slug=<s>             one review item: title/repo/issue + plan markdown
                                          (body only; the page renders at ``?p=review-<s>``)
    POST /api/review-action               Approve / Modify / Reject a review item (shared-key
                                          auth): drops a ``type: status-review-<action>``
                                          surface into SURFACE_DIR. The server never calls
                                          ``gh``; Speaking executes the GitHub side.
    POST /api/chat/react                  Add / remove an emoji reaction on a chat message (shared-key
                                          auth, body ``{at, emoji, action}``): appends to the
                                          ``.chat-reactions.jsonl`` sidecar, merged into ``GET /api/chat``
    POST /api/chat                        Jason's reply on the general Chat tab (shared-key auth):
                                          appends to ``.chat.jsonl`` (attachments stored under
                                          ``UPLOAD_DIR/chat/<date>/``, same rules as /api/reply) and drops a
                                          ``type: status-chat`` surface into SURFACE_DIR
                                          (no thread/item context — just the message).

Anything else is a 404: dotfiles (``.replies.jsonl``, ``.item-state.json``, ``.chat.jsonl``,
``.chat-reactions.jsonl``, ``.waiting-order.json``),
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
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from status_site._sse import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_TICK_SECONDS,
    SectionBroadcaster,
    check_key as _sse_check_key,
    stream_events as _sse_stream_events,
)
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
    pipeline as _pipeline,
    review_queue as _review_queue,
    running_now as _running_now,
    tasks as _tasks,
    waiting_on_you as _waiting_on_you,
)
from status_site.sources import (
    bugs as _bugs_page,
    chat as _chat_page,
    review as _review_page,
)
from status_site.sources.bugs import (
    format_pricing_segment as _format_pricing_segment,
    format_usd_local as _format_usd_local,
    sum_pricing as _sum_pricing,
)
from status_site.sources.common import (
    asks_path as _asks_path,
    read_asks as _read_asks,
)
from status_site import urgency as _urgency
from status_site.chat_reactions import (
    fold as reaction_fold,
    merge_reactions,
    read_events as reaction_events,
    reactions_for,
    valid_emoji,
    ATTENTION_EMOJI,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# The SSE-driven React shell at ``index.html`` is the only UI; it
# renders section updates live from ``/api/events``.
STATIC_FILES = {
    "index.html",
    "marked.min.js",
    "design-tokens.css",
    "chat-ux.js",
}

# Content files served from STATUS_DIR: flat names only (no '/', no leading dot).
CONTENT_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(md|json|png|jpe?g|gif|webp|txt)$"
)
CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
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
MAX_CHAT_HISTORY = 150
# Paginated ``GET /api/chat?before=<at>&limit=<N>``: default / cap page size.
CHAT_PAGE_DEFAULT = 50
CHAT_PAGE_MAX = 200
# Messages the SSE ``page:chat`` snapshot carries (the recent tail).
CHAT_SNAPSHOT_LIMIT = 50
# How many tail lines to splice into the Speaking surface for context.
CHAT_SURFACE_TAIL = 5
# Longest message ``at`` / ``reply_to`` accepted from a client.
MAX_MSG_AT = 64

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

REVIEW_SLUG_MAX = 60
REVIEW_ACTIONS = ("approve", "modify", "reject")

REVIEW_SPEAKING_FOOTERS = {
    "approve": (
        "For Speaking: Jason APPROVED this plan via the status site's Review page "
        "({url}?p=review-{slug}, key-authenticated). Execute the GitHub side: post "
        "`[SM] design-approved` on {ref}, move the issue to `sm:selected` / ship it, "
        "include his notes in the comment if any. Then move the item off *Waiting on you*."
    ),
    "modify": (
        "For Speaking: Jason wants this plan MODIFIED (Review page, "
        "{url}?p=review-{slug}). Execute the GitHub side: post his notes as an amendment "
        "comment on {ref} (`[SM] design-revise`) and move the issue back to `sm:designing`. "
        "Keep the item on *Waiting on you* until the revised plan is ready."
    ),
    "reject": (
        "For Speaking: Jason REJECTED this plan (Review page, {url}?p=review-{slug}). "
        "Execute the GitHub side: comment his notes on {ref}, close the issue and label it "
        "`sm:rejected`. Then move the item off *Waiting on you*."
    ),
}


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
    # SSE wiring (#639, slice-1). ``broadcaster`` fans Overview
    # section renders out to open ``GET /api/events`` connections.
    # ``None`` → the endpoint 404s (same spin-up pattern as
    # ``pollers``); tests construct a broadcaster explicitly so they
    # don't need background threads they didn't ask for.
    broadcaster: "SectionBroadcaster | None" = field(default=None, repr=False)
    # Injectable for the heartbeat-cadence regression test (pinned at
    # 0.1s). 15s matches the connectivity-guide default and most
    # reverse-proxy idle cutoffs.
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    # Broadcast tick cadence. Fast enough that "Jason added an ask"
    # lands on his screen within a second; slow enough that the render
    # doesn't dominate CPU on an idle box.
    sse_tick_seconds: float = DEFAULT_TICK_SECONDS

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
    def chat_reactions_log(self) -> Path:
        return self.status_dir / ".chat-reactions.jsonl"

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


def _chat_ts(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def read_chat(
    cfg: Config, limit: int = MAX_CHAT_HISTORY, before: str | None = None
) -> list[dict]:
    """General chat log (``.chat.jsonl``), newest-at-end, trimmed to ``limit``.

    With ``before`` (an ISO ``at`` timestamp) only messages strictly
    older than it are considered, so the result is the ``limit`` newest
    messages preceding ``before`` — still chronological, ready to
    prepend. An unparsable ``before`` yields ``[]`` (never the tail,
    which the client already has); messages with an unparsable ``at``
    are skipped in that mode.

    Rewrites each attachment's ``path`` into a ``url`` under
    ``/chat-upload/<date>/<filename>`` so the client can render images
    inline. ``path`` stays for existing consumers. Reactions from the
    ``.chat-reactions.jsonl`` sidecar are merged in as ``reactions``.
    """
    p = cfg.chat_log
    if not p.is_file():
        return []
    msgs: list[dict] = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            msgs.append(json.loads(line))
        except ValueError:
            pass
    if before is not None:
        cutoff = _chat_ts(before)
        if cutoff is None:
            return []
        msgs = [
            m
            for m in msgs
            if isinstance(m, dict)
            and (ts := _chat_ts(m.get("at"))) is not None
            and ts < cutoff
        ]
    msgs = msgs[-limit:] if limit else msgs
    # Extract the <date>/<filename> suffix from each attachment's path
    # so the client can build /chat-upload/<date>/<filename>. The stored
    # path may be any absolute host prefix (alias) ending in
    # ``.../chat/<date>/<filename>``; we don't care about the prefix.
    chat_suffix = re.compile(r"/chat/(\d{4}-\d{2}-\d{2}/[^/]+)$")
    for m in msgs:
        for att in m.get("attachments") or []:
            raw = att.get("path") or ""
            if not raw:
                continue
            match = chat_suffix.search(raw)
            if match:
                att["url"] = "/chat-upload/" + match.group(1)
    return merge_reactions(msgs, cfg.chat_reactions_log)


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
    cfg: Config,
    *,
    text: str,
    prior: list[dict],
    files: list[dict] | None = None,
    message_at: str = "",
) -> str:
    """Build the ``type: status-chat`` surface document (no thread/item fields).

    ``message_at`` is the chat-log ``at`` of the message; it lets the
    queue deep-link to ``?p=chat#msg-<at>``. Omitted → plain chat link.
    """
    message_line = f"message_at: {message_at}\n" if message_at else ""
    tail = "\n".join(f"  {m.get('from')}: {m.get('text')}" for m in prior)
    attached = "\n".join(f"  {x['path']} ({x['bytes']} bytes)" for x in files or [])
    return f"""---
priority: action
type: status-chat
reply_expected: true
created: {time.strftime("%Y-%m-%d")}
source: status-site
page: chat
{message_line}---

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
        at = now_iso()
        record = {
            "from": "jason",
            "at": at,
            "text": text,
            "files": [x["name"] for x in files],
            "attachments": files,
        }
        reply_to = d.get("reply_to")
        if isinstance(reply_to, str) and reply_to.strip():
            record["reply_to"] = reply_to.strip()[:MAX_MSG_AT]
        append_chat(cfg, record)

        doc = render_chat_surface(
            cfg, text=text, prior=prior, files=files, message_at=at
        )
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


_REACT_SURFACE_SEEN: dict[tuple[str, str], float] = {}
REACT_SURFACE_COOLDOWN_S = 60.0


def _drop_reaction_surface(cfg: Config, msg: dict, emoji: str, reacted_at: str) -> None:
    """Write a ``chat-reaction`` surface for an attention emoji on Alice's message."""
    key = (str(msg.get("at")), emoji)
    mono = time.monotonic()
    last = _REACT_SURFACE_SEEN.get(key)
    if last is not None and mono - last < REACT_SURFACE_COOLDOWN_S:
        return
    _REACT_SURFACE_SEEN[key] = mono
    at = str(msg.get("at"))
    doc = f"""---
priority: wander
type: chat-reaction
reply_expected: false
created: {time.strftime("%Y-%m-%d")}
source: status-site
page: chat
message_at: {at}
---

# Jason reacted {emoji} to one of your Chat messages

He flagged this message as needing a look. Decide whether to edit it,
send a clarifying follow-up, or just acknowledge in chat.

Your message:
{msg.get("text") or "(no text)"}

Reaction: {emoji} at {reacted_at}
Link: {cfg.public_url}?p=chat#msg-{at}
"""
    cfg.surface_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{time.strftime('%Y-%m-%dT%H-%M-%S')}-chat-reaction.md"
    tmp = cfg.surface_dir / f".tmp-react-{os.getpid()}-{time.time_ns()}"
    tmp.write_text(doc)
    dest = cfg.surface_dir / fname
    n = 1
    while dest.exists():
        n += 1
        dest = cfg.surface_dir / fname.replace(".md", f"-{n}.md")
    os.rename(tmp, dest)


def handle_chat_react(cfg: Config, d: dict) -> dict:
    """Add or remove one emoji reaction on a chat message (issue #668).

    Body: ``{key, at, emoji, action: "add"|"remove"}``. Appends an event to
    the ``.chat-reactions.jsonl`` sidecar (``.chat.jsonl`` is untouched);
    a no-op add/remove appends nothing. Returns the message's resulting
    ``reactions``. The SSE broadcaster re-reads the log every tick, so
    there is no cache to invalidate.
    """
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    at = d.get("at")
    emoji = d.get("emoji")
    action = d.get("action", "add")
    if not isinstance(at, str) or not at or len(at) > MAX_MSG_AT:
        raise ReplyError(400, "bad at")
    if not valid_emoji(emoji):
        raise ReplyError(400, "bad emoji")
    if action not in ("add", "remove"):
        raise ReplyError(400, "bad action")
    who = "jason"
    with cfg.lock:
        target = next((m for m in read_chat(cfg, limit=0) if m.get("at") == at), None)
        if target is None:
            raise ReplyError(404, "no such message")
        state = reaction_fold(reaction_events(cfg.chat_reactions_log))
        has = who in (state.get(at) or {}).get(emoji, {})
        if has != (action == "add"):
            cfg.status_dir.mkdir(parents=True, exist_ok=True)
            with open(cfg.chat_reactions_log, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "at": at,
                            "emoji": emoji,
                            "from": who,
                            "at_reacted": now_iso(),
                            "action": action,
                        }
                    )
                    + "\n"
                )
            state = reaction_fold(reaction_events(cfg.chat_reactions_log))
            if (
                action == "add"
                and emoji in ATTENTION_EMOJI
                and target.get("from") == "alice"
            ):
                _drop_reaction_surface(cfg, target, emoji, now_iso())
    return {"ok": True, "reactions": reactions_for(state, at)}


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
        (lb.get("name") if isinstance(lb, dict) else lb) for lb in item.get("labels") or []
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


def thinking_queue_rows(cache: dict | None, now: float | None = None) -> dict:
    """Typed twin of :func:`render_thinking_queue` for the Overview aside.

    ``{"available": bool, "rows": [...], "total": int}``. Rows follow the
    phase order then issue number and carry the same cap as the markdown.
    """
    now = time.time() if now is None else now
    if cache is None or now - cache["synced_at"] > THINKING_QUEUE_STALE_SECONDS:
        return {"available": False, "rows": [], "total": 0}
    buckets: dict[int, list[dict]] = {}
    for item in cache["items"]:
        phase = _thinking_phase(item)
        if phase is not None:
            buckets.setdefault(phase, []).append(item)
    rows: list[dict] = []
    total = sum(len(v) for v in buckets.values())
    for idx, (_, heading) in enumerate(THINKING_QUEUE_PHASES):
        for item in sorted(buckets.get(idx, []), key=lambda i: int(i["number"])):
            if len(rows) >= THINKING_QUEUE_CAP:
                break
            num = int(item["number"])
            rows.append(
                {
                    "phase": heading,
                    "number": num,
                    "title": " ".join(str(item.get("title", "")).split()),
                    "link": f"https://github.com/{THINKING_QUEUE_REPO}/issues/{num}",
                    "age": _format_age(item.get("updatedAt"), now),
                }
            )
    return {"available": True, "rows": rows, "total": total}


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
                "reported_at": (fm.get("reported_at") or "").strip(),
                "severity": (fm.get("severity") or fm.get("priority") or "").strip(),
                "updated_ts": st.st_mtime,
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
            fm, body = parse_frontmatter(text)
        except Exception:
            fm, body = {}, ""
        stype = (fm.get("type") or "").strip()
        out.append(
            {
                "slug": slug,
                "type": stype,
                "message_at": (fm.get("message_at") or "").strip(),
                "preview": (
                    _chat_page.chat_preview(body) if stype == "status-chat" else ""
                ),
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


def _chat_link(item: dict) -> str:
    """``?p=chat#msg-<at>`` when the surface knows its message, else ``?p=chat``."""
    at = item.get("message_at", "")
    return f"?p=chat#msg-{at}" if at else "?p=chat"


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
        return _chat_link(item)
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
        return f"[open chat]({_chat_link(item)})"
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
        if it.get("preview"):
            line += f" — “{_md_escape(it['preview'])}”"
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

TRACKED_TASKS_START = "<!-- tracked-tasks:start -->"
TRACKED_TASKS_END = "<!-- tracked-tasks:end -->"


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


def _tracked_tasks_fallback(rendered: str) -> str:
    """Appended-section shape for the SM v2 task list (issue #780).

    Lives between ``Alice's queue`` and ``Thinking queue`` so repo-less
    todos sit next to the GH-backed pipeline without being mixed into
    it. The marker-less fallback keeps the page serveable while the
    one-time index.md edit that inserts the markers catches up.
    """
    return (
        "\n## Tracked tasks\n"
        f"{TRACKED_TASKS_START}\n{rendered}\n{TRACKED_TASKS_END}\n"
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
DEFAULT_PIPELINE_POLL_SECONDS = 30
DEFAULT_DONE_TODAY_POLL_SECONDS = 300


def _tasks_dir() -> Path:
    return (
        Path(os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind")))
        / "inner"
        / "tasks"
    )


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
        pipeline_poll: float = DEFAULT_PIPELINE_POLL_SECONDS,
        stop: threading.Event | None = None,
    ):
        self.cfg = cfg
        self._cache: dict[str, _CachedValue] = {}
        self._review_poll = review_poll
        self._running_now_poll = running_now_poll
        self._done_today_poll = done_today_poll
        self._pipeline_poll = pipeline_poll
        self.stop = stop or threading.Event()
        self._threads: list[threading.Thread] = []

    # ── cache accessors ─────────────────────────────────────────
    def get(self, key: str) -> object | None:
        with self.cfg.lock:
            entry = self._cache.get(key)
        return entry.value if entry else None

    def fetched_at(self) -> dict[str, float]:
        """``{source: epoch of last successful poll}`` for system-health."""
        with self.cfg.lock:
            return {k: v.fetched_at for k, v in self._cache.items()}

    def _set(self, key: str, value: object) -> None:
        with self.cfg.lock:
            self._cache[key] = _CachedValue(value=value, fetched_at=time.time())

    # ── background loops ────────────────────────────────────────
    def _loop_running_now(self) -> None:
        repo_dir = _running_now.repo_dir_from_env()
        tasks_dir = _tasks_dir()
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

    def _loop_pipeline(self) -> None:
        while not self.stop.is_set():
            try:
                repos = _pipeline.repos_sm_from_config()
                issues, failed = _pipeline.fetch_issues(repos)
                self._set("pipeline", {"issues": issues, "failed": failed})
                # Issue #719: synthesise review notes for every open
                # ``sm:design_review`` + ``art:code`` issue so the
                # Review tab has something to render. Isolate failures
                # so a bad disk write doesn't stop the pipeline cache
                # from updating.
                try:
                    _pipeline.ensure_review_notes(
                        issues, self.cfg.status_dir / "notes"
                    )
                except Exception as e:  # noqa: BLE001
                    print(
                        f"status_site: ensure_review_notes failed: {e!r}",
                        flush=True,
                    )
            except Exception as e:  # noqa: BLE001
                print(f"status_site: pipeline poll failed: {e!r}", flush=True)
            self.stop.wait(self._pipeline_poll)

    def start(self) -> None:
        for name, target in (
            ("running-now-poll", self._loop_running_now),
            ("pipeline-poll", self._loop_pipeline),
            ("review-queue-poll", self._loop_review_queue),
            ("done-today-poll", self._loop_done_today),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)


# ─── per-section markdown renderers ───────────────────────────────
#
# One place that knows how to turn each source's state into markdown.
# ``/index.md`` (via ``overview_splice_sections``) and the SSE
# broadcaster (via ``SectionBroadcaster`` in ``_sse.py``) both call
# through here, so Overview on the markdown page and Overview on the
# reactive shell are byte-identical. Section names are the stable
# identifiers used on SSE ``section.update`` events; keep them in
# sync with the client's reducer.


_DEPLOY_TTL_SECONDS = 60.0
_deploy_cache: dict[str, object] = {"at": 0.0, "value": None}


def deploy_status() -> dict:
    """Commit the status-site checkout is serving, on this host (60s cache).

    ``{"host", "sha", "subject", "committed"}``; ``sha`` is ``""`` when the
    repo can't be queried (no git, not a checkout).
    """
    now = time.time()
    cached = _deploy_cache["value"]
    if cached is not None and now - float(_deploy_cache["at"]) < _DEPLOY_TTL_SECONDS:
        return cached  # type: ignore[return-value]
    value = {"host": socket.gethostname(), "sha": "", "subject": "", "committed": ""}
    try:
        out = subprocess.run(
            ["git", "-C", str(_running_now.repo_dir_from_env()), "log", "-1",
             "--format=%h%x00%cI%x00%s"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        sha, committed, subject = (out.split("\0") + ["", "", ""])[:3]
        value.update(sha=sha, committed=committed, subject=subject)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    _deploy_cache.update(at=now, value=value)
    return value


def section_renderers(cfg: "Config") -> dict[str, Callable[[], dict]]:
    """Return ``{section_name: () -> payload}`` for every Overview section.

    Payloads are typed dicts shaped per #649: every renderer at least
    emits ``{"markdown": "..."}`` so the generic markdown fallback in
    ``SectionTile`` keeps rendering sections the shell has no typed
    widget for yet; the four slice-2 sections (running_now, waiting_on_you,
    alice_queue, done_today) also carry typed ``rows`` so widgets can
    render without re-parsing markdown. Dict equality is deep, so the
    broadcaster's change-detection compare still works unchanged.

    Closures read live state each call: TTL-cached sources via
    ``cfg.pollers`` (an empty cache renders the "nothing yet" string so
    the SSE snapshot and ``/index.md`` both stay serveable before the
    first poll fires); cheap sources straight off disk. The
    ``alice_queue`` closure scans surfaces on each call — fine for
    broadcasting (surfaces dir is small), and the
    ``/index.md`` path passes its own pre-scanned ``items`` through
    :func:`overview_splice_sections` to avoid double-scanning per
    request.
    """
    pollers = cfg.pollers

    def _render_running_now() -> dict:
        rows = (pollers.get("running_now") if pollers else None) or []
        md = _running_now.render(rows)
        typed_rows: list[dict] = []
        for t in rows:  # type: ignore[attr-defined]
            try:
                typed_rows.append(asdict(t))
            except TypeError:
                # Already a dict (defensive: shouldn't happen with the
                # frozen dataclass, but we never want a payload coerce
                # to blow up the SSE tick).
                typed_rows.append(dict(t) if isinstance(t, dict) else {})
        return {"markdown": md, "rows": typed_rows}

    def _render_ready_for_review() -> dict:
        cached = (pollers.get("review_queue") if pollers else None) or {}
        md = _review_queue.render(
            cached.get("design", []),
            cached.get("pr", []),
            design_failed=cached.get("design_failed", []),
            pr_failed=cached.get("pr_failed", []),
        )
        return {"markdown": md}

    def _ask_rows() -> list[dict]:
        asks = _read_asks(_asks_path(cfg.status_dir))
        pending = _waiting_on_you.unresolved_asks(asks)
        rows: list[dict] = []
        for r in pending:
            rank = _urgency.classify(r)
            rows.append(
                {
                    "id": r.get("id", ""),
                    "text": str(r.get("text") or "").strip(),
                    "created": r.get("created", ""),
                    "urgency": rank,
                }
            )
        # Stable NOW-first sort (NOW rises above UNSET rises above
        # TODAY, oldest within a rank); existing markdown render keeps
        # its own ordering for /index.md parity.
        rows.sort(
            key=lambda r: (
                _urgency.RANK_ORDER.get(r["urgency"], len(_urgency.RANK_ORDER)),
                r.get("created") or "",
            )
        )
        return rows

    def _surface_rows(items: list[dict], now: float) -> list[dict]:
        return [
            {
                "slug": it.get("slug", ""),
                "type": it.get("type", ""),
                "label": _surface_label(it),
                "link": _surface_link_url(it),
                "preview": it.get("preview", ""),
                "age_seconds": now - float(it.get("created_epoch", now)),
                "created": it.get("created", ""),
            }
            for it in items
        ]

    def _render_waiting_on_you() -> dict:
        hand_zone_md = _waiting_on_you.read_hand_zone(
            cfg.status_dir / "waiting-on-you.md"
        )
        rows = _ask_rows()
        md = _waiting_on_you.render(
            cfg.status_dir / "waiting-on-you.md",
            asks_path=_asks_path(cfg.status_dir),
        )
        return {
            "markdown": md,
            "hand_zone_md": hand_zone_md,
            "rows": rows,
        }

    def _render_alice_queue() -> dict:
        # ``rows`` stays the surfaces list (the pre-#653 contract);
        # ``pipeline`` (GH issues) and ``tasks`` are the new groups.
        items = scan_surfaces(cfg.surface_dir)
        now = time.time()
        rows = _surface_rows(items, now)
        cached = (pollers.get("pipeline") if pollers else None) or {}
        pipeline_rows = _pipeline.issue_rows(cached.get("issues") or [], now=now)
        task_rows = _pipeline.scan_tasks(_tasks_dir(), now=now)
        if pipeline_rows or task_rows:
            md = _pipeline.render_markdown(pipeline_rows, rows, task_rows)
        else:
            md = render_alice_queue(items, now=now)
        return {
            "markdown": md,
            "rows": rows,
            "pipeline": pipeline_rows,
            "tasks": task_rows,
            "pipeline_failed": list(cached.get("failed") or []),
        }

    def _render_done_today() -> dict:
        cached = (pollers.get("done_today") if pollers else None) or {}
        today = cached.get("today") or _done_today.today_local()
        asks = _read_asks(_asks_path(cfg.status_dir))
        resolved = _done_today.resolved_today(asks, today)
        md = _done_today.render(cached.get("prs", []), resolved)
        prs_rows: list[dict] = []
        for pr in cached.get("prs", []) or []:  # type: ignore[attr-defined]
            try:
                prs_rows.append(asdict(pr))
            except TypeError:
                if isinstance(pr, dict):
                    prs_rows.append(dict(pr))
        asks_rows: list[dict] = [
            {
                "id": r.get("id", ""),
                "text": str(r.get("text") or "").strip(),
                "resolved_at": r.get("resolved_at", ""),
            }
            for r in resolved
        ]
        return {
            "markdown": md,
            "prs": prs_rows,
            "asks_resolved": asks_rows,
        }

    def _render_known_issues() -> dict:
        return {"markdown": _known_issues.render(cfg.status_dir / "known-issues.md")}

    def _render_tracked_tasks() -> dict:
        """Live scan of the SM v2 task store (issue #780).

        Scan is cheap (a few hundred tiny YAML files, max) so we skip
        the TTL cache and read on each tick. Tasks already tracked via
        a GitHub issue are filtered out inside :func:`scan_tasks` so
        the GH-pipeline source stays the single owner of those rows.
        """
        rows = _tasks.scan_tasks(_tasks_dir(), now=time.time())
        md = _tasks.render(rows)
        return {"markdown": md, "rows": _tasks.typed_rows(rows)}

    # ── Overview tiers by who owns the next action (issue #691) ────
    # ``your_todo`` / ``in_flight`` / ``thinking_queue`` / ``system_health``
    # are the sections the reactive shell draws; the older per-source
    # sections above stay because ``/index.md`` splices and existing
    # consumers read them.

    def _render_your_todo() -> dict:
        now = time.time()
        review = (pollers.get("review_queue") if pollers else None) or {}
        pipe = (pollers.get("pipeline") if pollers else None) or {}
        owner = _pipeline.split_by_owner(
            _pipeline.issue_rows(pipe.get("issues") or [], now=now)
        )
        pr_rows = _pipeline.sort_rows(
            [_pipeline.review_item_row(i) for i in review.get("pr") or []]
        )
        design_rows = _pipeline.merge_design_review(
            owner["jason"], review.get("design") or []
        )
        # Issue #719: tag design-review rows whose GH issue has a
        # synthesised review note so the frontend can link the title
        # to ``?p=review-<slug>`` instead of straight to GitHub. The
        # review note file's stem == the slug (``read_review`` opens
        # ``notes/<slug>.md`` directly).
        notes_dir = cfg.status_dir / "notes"
        if notes_dir.is_dir():
            existing_slugs = {p.stem for p in notes_dir.glob("*.md")}
            for row in design_rows:
                repo = str(row.get("repo") or "")
                number = row.get("number")
                if not repo or not number:
                    continue
                slug = _pipeline.review_slug_for(repo, number)
                if slug in existing_slugs:
                    row["review_slug"] = slug
        ask_rows = _ask_rows()
        your_todo_md = _waiting_on_you.read_hand_zone(
            cfg.status_dir / "waiting-on-you.md"
        )
        external_todos_md = _waiting_on_you.read_hand_zone(
            cfg.status_dir / "external-todos.md"
        )
        # Attention-reacts (👀 ⚠️ 👎 ❓) wait on #683; the key is the
        # contract so the widget lights up as soon as it is populated.
        attention_reacts: list[dict] = []
        counts = {
            "prs": len(pr_rows),
            "design_review": len(design_rows),
            "attention_reacts": len(attention_reacts),
            "asks": len(ask_rows) + (1 if your_todo_md else 0),
            "external": 1 if external_todos_md else 0,
        }
        total = sum(counts.values())
        return {
            "markdown": f"{total} items waiting on you" if total else "*(nothing waiting on you)*",
            "prs": pr_rows,
            "design_review": design_rows,
            "attention_reacts": attention_reacts,
            "rows": ask_rows,
            "your_todo_md": your_todo_md,
            "external_todos_md": external_todos_md,
            "counts": counts,
            "failed": {
                "prs": list(review.get("pr_failed") or []),
                "design_review": list(review.get("design_failed") or []),
            },
        }

    def _render_in_flight() -> dict:
        now = time.time()
        running = _render_running_now()
        pipe = (pollers.get("pipeline") if pollers else None) or {}
        owner = _pipeline.split_by_owner(
            _pipeline.issue_rows(pipe.get("issues") or [], now=now)
        )
        surfaces = _surface_rows(scan_surfaces(cfg.surface_dir), now)
        tasks = _pipeline.scan_tasks(_tasks_dir(), now=now)
        n = len(running["rows"]) + len(owner["alice"]) + len(surfaces) + len(tasks)
        return {
            "markdown": f"{n} in flight" if n else "*(nothing in flight)*",
            "running": running["rows"],
            "pipeline": owner["alice"],
            "surfaces": surfaces,
            "tasks": tasks,
            "pipeline_failed": list(pipe.get("failed") or []),
        }

    def _render_thinking_queue() -> dict:
        data = thinking_queue_rows(read_thinking_queue(cfg.status_dir))
        md = (
            f"{data['total']} in thinking queue"
            if data["rows"]
            else "*(thinking queue unavailable or empty)*"
        )
        return {"markdown": md, **data}

    def _render_system_health() -> dict:
        fetched = pollers.fetched_at() if pollers else {}
        known = _known_issues.render(cfg.status_dir / "known-issues.md")
        return {
            "markdown": known,
            "known_issues_md": known,
            "deploy": [deploy_status()],
            "pollers": [
                {"name": k, "fetched_at": v} for k, v in sorted(fetched.items())
            ],
        }

    # A2UI-rendered pages (issue #651). Each ``page:<slug>`` section
    # emits a ``{"a2ui": [envelope, ...]}`` payload the client walks
    # through its generic surface renderer. The broadcaster's dict-
    # equality compare treats these payloads the same as any other
    # section payload — unchanged → no event fires.
    def _render_page_chat() -> dict:
        return _chat_page.render(cfg)

    def _render_page_bugs() -> dict:
        return _bugs_page.render(cfg.status_dir)

    def _render_page_review() -> dict:
        return _review_page.render(cfg)

    # Preserve Overview order; the SSE client iterates this dict to
    # draw sections top-to-bottom. Page sections live alongside
    # Overview sections in the same snapshot — the client only mounts
    # ``page:<slug>`` for the currently-active hash route, so idle
    # pages cost one dict-equality check per tick.
    return {
        "your_todo": _render_your_todo,
        "in_flight": _render_in_flight,
        "thinking_queue": _render_thinking_queue,
        "system_health": _render_system_health,
        "running_now": _render_running_now,
        "ready_for_review": _render_ready_for_review,
        "waiting_on_you": _render_waiting_on_you,
        "alice_queue": _render_alice_queue,
        "tracked_tasks": _render_tracked_tasks,
        "done_today": _render_done_today,
        "known_issues": _render_known_issues,
        "page:chat": _render_page_chat,
        "page:bugs": _render_page_bugs,
        "page:review": _render_page_review,
    }


def overview_splice_sections(
    cfg: "Config",
    *,
    items: list[dict],
    now: float,
) -> list[tuple]:
    """Build the ``_splice_sections`` list for ``GET /index.md``.

    Order mirrors the Overview as it appears on the page:
    Running now → Ready for Review → Waiting on you → Alice's queue →
    Tracked tasks → Done today → Known issues I'm on. (``## Thinking
    queue`` is spliced separately by :func:`splice_thinking_queue`
    directly from the hand-edit position in ``index.md``, so it slots
    between Tracked tasks and Done today in the rendered page without
    appearing in this list.) Each tuple carries sentinel markers, a
    renderer closure, and a fallback builder so the append-case keeps
    the page serveable while the mind repo catches up on the
    marker-insertion pass.

    Renderers are shared with the SSE broadcaster via
    :func:`section_renderers`. The ``alice_queue`` tuple below passes
    the caller's pre-scanned ``items`` + ``now`` into
    :func:`render_alice_queue` directly — ``/index.md`` already
    collected them to compute Last-Modified, so we skip the extra
    disk scan that the SSE path's closure does.
    """
    renderers = section_renderers(cfg)

    def _md(name: str) -> Callable[[], str]:
        """Adapt a typed-dict renderer to the markdown-string shape
        ``_splice_sections`` wants. Falsy / missing ``markdown`` → ``""``
        so the /index.md path stays serveable even if a renderer only
        returns typed rows.
        """
        fn = renderers[name]
        return lambda: str((fn() or {}).get("markdown") or "")

    return [
        (
            RUNNING_NOW_START,
            RUNNING_NOW_END,
            _md("running_now"),
            _running_now_fallback,
        ),
        (
            READY_FOR_REVIEW_START,
            READY_FOR_REVIEW_END,
            _md("ready_for_review"),
            _ready_for_review_fallback,
        ),
        (
            WAITING_ON_YOU_START,
            WAITING_ON_YOU_END,
            _md("waiting_on_you"),
            _waiting_on_you_fallback,
        ),
        (
            ALICE_QUEUE_START,
            ALICE_QUEUE_END,
            lambda: render_alice_queue(items, now=now),
            _alice_queue_fallback,
        ),
        (
            TRACKED_TASKS_START,
            TRACKED_TASKS_END,
            _md("tracked_tasks"),
            _tracked_tasks_fallback,
        ),
        (
            DONE_TODAY_START,
            DONE_TODAY_END,
            _md("done_today"),
            _done_today_fallback,
        ),
        (
            KNOWN_ISSUES_START,
            KNOWN_ISSUES_END,
            _md("known_issues"),
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
        _atomic_write(index_path, "\n".join(ln for i, ln in enumerate(lines) if i not in drop))
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

# ---------------------------------------------------------------- reviews
#
# Review-shaped items are ``STATUS_DIR/notes/<slug>.md`` files whose frontmatter
# carries ``kind: decide`` or ``needs: review`` (written by the gh watcher on
# ``→ sm:design_review``, or by Speaking for stopgap/decide filings). The plan
# body is embedded in the note, so the Review page renders without ``gh``.


def _review_slug(raw: object) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(raw).lower())[:REVIEW_SLUG_MAX]


def _is_review_frontmatter(fm: dict[str, str]) -> bool:
    return (
        fm.get("kind", "").strip().lower() == "decide"
        or fm.get("needs", "").strip().lower() == "review"
    )


def read_review(cfg: Config, slug: str) -> dict | None:
    """One review item, or ``None`` if the note is missing / not review-shaped."""
    slug = _review_slug(slug)
    if not slug:
        return None
    p = cfg.status_dir / "notes" / f"{slug}.md"
    try:
        text = p.read_text(errors="replace")[:200_000]
        mtime = p.stat().st_mtime
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    if not _is_review_frontmatter(fm):
        return None
    return {
        "slug": slug,
        "title": fm.get("title", "") or slug,
        "repo": fm.get("repo", ""),
        "issue": fm.get("issue", ""),
        "url": fm.get("url", ""),
        "markdown": body.strip(),
        "updated": mtime,
    }


def list_reviews(cfg: Config) -> list[str]:
    """Slugs of every review-shaped note under ``STATUS_DIR/notes``."""
    notes = cfg.status_dir / "notes"
    if not notes.is_dir():
        return []
    out = []
    for p in sorted(notes.glob("*.md")):
        if p.name.startswith("."):
            continue
        try:
            fm, _ = parse_frontmatter(p.read_text(errors="replace")[:20_000])
        except OSError:
            continue
        if _is_review_frontmatter(fm):
            out.append(p.stem)
    return out


def render_review_surface(
    cfg: Config, *, action: str, review: dict, notes: str
) -> str:
    """Build the ``type: status-review-<action>`` surface document."""
    slug = review["slug"]
    ref = (
        f"{review['repo']}#{review['issue']}"
        if review.get("repo") and review.get("issue")
        else slug
    )
    footer = REVIEW_SPEAKING_FOOTERS[action].format(
        url=cfg.public_url, slug=slug, ref=ref
    )
    return f"""---
priority: action
type: status-review-{action}
reply_expected: false
created: {time.strftime("%Y-%m-%d")}
detail: status-review-{slug}
source: status-site
page: review-{slug}
review: {slug}
repo: {review.get("repo", "")}
issue: {review.get("issue", "")}
action: {action}
---

# Jason chose to {action.upper()} a plan on the status page

Item: {review["title"]}
Reference: {ref}
Plan link: {review.get("url") or "(none)"}

His notes:
{notes or "(no notes)"}

---
{footer}
"""


def _sm_comment_body(action: str, notes: str) -> str | None:
    """Comment body for ``gh issue comment`` or ``None`` if nothing to post.

    Issue #719 contract:

    * ``approve`` → ``[SM] select art=code`` + a note that Jason acted
      from the status-site Review tab.
    * ``reject``  → ``[SM] reject reason=<notes>`` (reason falls back
      to a default phrase when Jason provided no text).
    * ``modify``  → free-form comment with Jason's notes and a footer
      that keeps the issue at ``sm:design_review`` for revision.
    """
    text = notes.strip()
    if action == "approve":
        verb = "[SM] select art=code"
        return f"{verb}\n\nJason approved via status-site Review tab."
    if action == "reject":
        reason = text or "(no reason given)"
        verb = f"[SM] reject reason={reason}"
        return f"{verb}\n\nJason rejected via status-site Review tab."
    if action == "modify":
        return (
            f"{text or '(see status-site Review tab)'}\n\n"
            "(Change request via status-site Review tab — leaving at "
            "sm:design_review for revision.)"
        )
    return None


# Dependency-inject-able; tests patch this to assert the gh invocation
# without shelling out. Takes ``(repo, number, body)`` and returns a
# ``subprocess.CompletedProcess``-ish object; raise on hard failure.
_GhCommentRunner = Callable[[str, int, str], subprocess.CompletedProcess]


def _run_gh_issue_comment(repo: str, number: int, body: str) -> subprocess.CompletedProcess:
    """Default ``gh issue comment`` invoker used by :func:`handle_review_action`."""
    return subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            repo,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _post_review_to_gh(
    review: dict,
    action: str,
    notes: str,
    *,
    runner: _GhCommentRunner | None = None,
) -> dict:
    """Post Jason's decision back to GitHub as an SM verb comment.

    Returns ``{"posted": bool, "error": str | None}``. ``posted`` is
    True only on a successful gh invocation; empty repo/issue (non-GH
    review notes), a non-zero gh exit, or a subprocess exception all
    return ``{"posted": False, "error": "<reason>"}``.

    Never raises — the local decision is already recorded; a failed
    comment should not turn a 200 into a 500.
    """
    repo = str(review.get("repo") or "").strip()
    issue = str(review.get("issue") or "").strip()
    if not repo or not issue:
        return {"posted": False, "error": None}  # non-GH review, by design
    body = _sm_comment_body(action, notes)
    if body is None:
        return {"posted": False, "error": f"no body for action {action!r}"}
    try:
        number = int(issue)
    except (TypeError, ValueError):
        return {"posted": False, "error": f"bad issue number {issue!r}"}
    try:
        result = (runner or _run_gh_issue_comment)(repo, number, body)
    except (OSError, subprocess.SubprocessError) as e:
        return {"posted": False, "error": f"gh invocation failed: {e!r}"}
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        return {"posted": False, "error": f"gh returncode={result.returncode}: {err}"}
    return {"posted": True, "error": None}


def handle_review_action(cfg: Config, d: dict) -> dict:
    """Validate one Approve / Modify / Reject click and drop its surface.

    Mirrors ``handle_reply``: key-authed, writes into ``SURFACE_DIR`` for
    Speaking. The review note itself is left alone — the item stays on
    *Waiting on you* until Speaking acts and updates the status index.

    Issue #719: for GH-issue-backed reviews the decision is also
    posted directly to the originating issue as an SM verb comment.
    The comment failure is non-fatal — the surface and local record
    still succeed.
    """
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    action = str(d.get("action", "")).lower()
    if action not in REVIEW_ACTIONS:
        raise ReplyError(400, "bad action")
    review = read_review(cfg, str(d.get("slug", "")))
    if review is None:
        raise ReplyError(404, "no such review")
    notes = str(d.get("notes", "")).strip()[:MAX_TEXT]
    if action == "modify" and not notes:
        raise ReplyError(400, "say what to change")

    doc = render_review_surface(cfg, action=action, review=review, notes=notes)
    with cfg.lock:
        cfg.surface_dir.mkdir(parents=True, exist_ok=True)
        fname = (
            f"{time.strftime('%Y-%m-%dT%H-%M-%S')}-status-review-{action}-"
            f"{review['slug']}.md"
        )
        tmp = cfg.surface_dir / f".tmp-review-{os.getpid()}-{time.time_ns()}"
        tmp.write_text(doc)
        dest = cfg.surface_dir / fname
        n = 1
        while dest.exists():
            n += 1
            dest = cfg.surface_dir / fname.replace(".md", f"-{n}.md")
        os.rename(tmp, dest)

    gh_result = _post_review_to_gh(
        review, action, notes, runner=getattr(cfg, "_gh_comment_runner", None)
    )
    resp: dict = {"ok": True, "action": action, "slug": review["slug"]}
    if gh_result.get("posted"):
        resp["gh_posted"] = True
    elif gh_result.get("error"):
        resp["gh_error"] = gh_result["error"]
    return resp


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

    def _sse(self, head: bool) -> None:
        """Serve ``GET /api/events`` — long-lived Server-Sent Events.

        Key auth via ``?k=<key>`` (EventSource can't set headers; even
        though the client uses fetch, the key still rides in the URL
        so shape + reconnects stay URL-only). The broadcaster is
        attached at boot — if it isn't wired yet, we 404 so tests that
        spin up a bare server don't need SSE plumbing they didn't ask
        for.
        """
        broadcaster = self.cfg.broadcaster
        if broadcaster is None:
            return self._json(404, {"error": "events disabled"}, head=head)
        q = parse_qs(urlsplit(self.path).query)
        key = (q.get("k") or [""])[0]
        if not _sse_check_key(self.cfg.key, key):
            return self._json(403, {"error": "bad key"}, head=head)
        if head:
            # A probe can check the endpoint is up without pinning a
            # connection — same headers as the full GET, no body.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        # These headers are the connectivity-guide recipe:
        # ``no-transform`` keeps intermediaries from buffering,
        # ``X-Accel-Buffering: no`` turns off nginx's accrual buffer.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        # Opening comment is a cheap way to flush the headers through
        # any TLS buffer + prove the stream is alive before the
        # snapshot lands. Clients drop ``:`` comments.
        try:
            self.wfile.write(b": alice-sse v1\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        try:
            _sse_stream_events(
                self.wfile,
                broadcaster,
                heartbeat_seconds=self.cfg.heartbeat_seconds,
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        # The stream closed — don't keep the TCP connection warm for a
        # follow-up request on this socket.
        self.close_connection = True

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
        if path == "/api/reviews":
            return self._json(200, list_reviews(self.cfg), head=head)
        if path == "/api/review":
            q = parse_qs(urlsplit(self.path).query)
            slug = (q.get("slug") or [""])[0]
            review = read_review(self.cfg, slug)
            if review is None:
                return self._json(404, {"error": "no review"}, head=head)
            # Side-effect for the A2UI ``page:review`` renderer
            # (issue #651): record the active slug so the next SSE
            # tick emits a surface for this item. The renderer is
            # generic — one entry in SectionBroadcaster — so the slug
            # lives in module state rather than as a per-slug renderer
            # key. See ``sources/review.py`` for the trade-off.
            _review_page.set_active_slug(review["slug"])
            return self._json(200, review, head=head)
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
            q = parse_qs(urlsplit(self.path).query)
            before = (q.get("before") or [None])[0]
            if before is None:
                return self._json(
                    200, {"messages": read_chat(self.cfg)}, head=head
                )
            try:
                limit = int((q.get("limit") or [CHAT_PAGE_DEFAULT])[0])
            except ValueError:
                limit = CHAT_PAGE_DEFAULT
            if limit < 1:
                limit = CHAT_PAGE_DEFAULT
            limit = min(limit, CHAT_PAGE_MAX)
            return self._json(
                200,
                {"messages": read_chat(self.cfg, limit=limit, before=before)},
                head=head,
            )
        if path.startswith("/chat-upload/"):
            q = parse_qs(urlsplit(self.path).query)
            key = (q.get("k") or [""])[0]
            if not _sse_check_key(self.cfg.key, key):
                return self._json(403, {"error": "bad key"}, head=head)
            # Strip the leading segment, split <date>/<filename>, reject
            # anything with '..' or '/' in the filename part.
            rel = unquote(path[len("/chat-upload/"):])
            parts = rel.split("/")
            if len(parts) != 2 or ".." in parts or not parts[0] or not parts[1]:
                return self._json(404, {"error": "not found"}, head=head)
            date_part, fname = parts
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
                return self._json(404, {"error": "not found"}, head=head)
            if "/" in fname or fname.startswith(".") or "\0" in fname:
                return self._json(404, {"error": "not found"}, head=head)
            fpath = (self.cfg.upload_dir / "chat" / date_part / fname).resolve()
            uploads_root = (self.cfg.upload_dir / "chat").resolve()
            try:
                fpath.relative_to(uploads_root)
            except ValueError:
                return self._json(404, {"error": "not found"}, head=head)
            if not fpath.is_file():
                return self._json(404, {"error": "not found"}, head=head)
            ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
            return self._send(
                200, fpath.read_bytes(), ctype,
                mtime=fpath.stat().st_mtime, head=head,
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
        if path == "/api/events":
            return self._sse(head=head)
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
            "/api/chat/react": handle_chat_react,
            "/api/waiting-order": handle_waiting_order,
            "/api/review-action": handle_review_action,
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
            pipeline_poll=float(
                os.environ.get(
                    "PIPELINE_POLL_SECONDS",
                    str(DEFAULT_PIPELINE_POLL_SECONDS),
                )
            ),
        )
        cfg.pollers = pollers
        pollers.start()
    if os.environ.get("ARCHIVE_ON_ROLL", "1") != "0":
        start_archive_roller(
            cfg, float(os.environ.get("ARCHIVE_INTERVAL", str(ARCHIVE_INTERVAL)))
        )
    if os.environ.get("SSE_BROADCAST", "1") != "0":
        # Snapshot-first SSE for #639 slice-1. Tick cadence is short
        # (2s default) but each render just formats already-cached
        # data, so CPU on an idle box is in the noise.
        cfg.heartbeat_seconds = float(
            os.environ.get("SSE_HEARTBEAT_SECONDS", str(cfg.heartbeat_seconds))
        )
        cfg.sse_tick_seconds = float(
            os.environ.get("SSE_TICK_SECONDS", str(cfg.sse_tick_seconds))
        )
        broadcaster = SectionBroadcaster(
            section_renderers(cfg),
            tick_seconds=cfg.sse_tick_seconds,
        )
        cfg.broadcaster = broadcaster
        broadcaster.start()
    print(f"status_site: serving {cfg.status_dir} on {cfg.host}:{cfg.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
