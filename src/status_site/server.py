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
                                          appends to ``.chat.jsonl`` and drops a
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from status_site.history import Tracker, read_history

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


def render_chat_surface(cfg: Config, *, text: str, prior: list[dict]) -> str:
    """Build the ``type: status-chat`` surface document (no thread/item fields)."""
    tail = "\n".join(f"  {m.get('from')}: {m.get('text')}" for m in prior)
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
{text}

Recent chat (for context, last {CHAT_SURFACE_TAIL} lines before this message):
{tail or "  (none: this is the first message)"}

---
{CHAT_SPEAKING_FOOTER.format(url=cfg.public_url)}
"""


def handle_chat(cfg: Config, d: dict) -> dict:
    """Validate + persist one chat message. Raises ReplyError on client errors.

    Mirrors ``handle_reply`` but with no thread, item, or attachments: the Chat
    tab is a single running log Jason uses when there's no todo item to anchor
    the conversation to.
    """
    if not isinstance(d, dict):
        raise ReplyError(400, "bad json")
    if not hmac.compare_digest(str(d.get("key", "")).encode(), cfg.key.encode()):
        raise ReplyError(403, "bad key")
    text = str(d.get("text", "")).strip()[:MAX_TEXT]
    if not text:
        raise ReplyError(400, "empty message")

    with cfg.lock:
        prior = read_chat(cfg)[-CHAT_SURFACE_TAIL:]
        append_chat(cfg, {"from": "jason", "at": now_iso(), "text": text})

        doc = render_chat_surface(cfg, text=text, prior=prior)
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


class ReplyError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


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
    if not isinstance(raw_files, list):
        raise ReplyError(400, "bad attachment")
    if len(raw_files) > MAX_FILES:
        raise ReplyError(413, f"at most {MAX_FILES} attachments")
    blobs = []
    for a in raw_files:
        if not isinstance(a, dict):
            raise ReplyError(400, "bad attachment")
        name = safe_filename(a.get("name"))
        try:
            blob = base64.b64decode(str(a.get("data", "")), validate=True)
        except (ValueError, binascii.Error):
            raise ReplyError(400, "bad attachment") from None
        if len(blob) > cfg.max_upload:
            raise ReplyError(413, f"{name} over {cfg.max_upload // (1024 * 1024)} MB")
        blobs.append((name, blob))
    if not text and not blobs:
        raise ReplyError(400, "empty reply")

    with cfg.lock:
        files = []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for i, (name, blob) in enumerate(blobs):
            ddir = cfg.upload_dir / tid
            ddir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = ddir / f"{stamp}-{i}-{name}"
            with open(path, "xb") as f:
                f.write(blob)
            alias = os.path.join(
                cfg.upload_dir_alias, os.path.relpath(path, cfg.upload_dir)
            )
            files.append({"name": name, "path": alias, "bytes": len(blob)})

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
BUG_STATUSES = ("open", "in-progress", "fixed", "wontfix", "dispatch-failed")
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
            }
        )
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
            if key == "in-progress" and pr_url:
                summary_bits.append(f"[draft PR]({pr_url})")
            pricing_segment = _format_pricing_segment(
                b.get("estimate_usd", ""), b.get("actual_usd", "")
            )
            if pricing_segment:
                summary_bits.append(pricing_segment)
            if summary_bits:
                line += ": *" + " — ".join(summary_bits) + "*"
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
    print(f"status_site: serving {cfg.status_dir} on {cfg.host}:{cfg.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
