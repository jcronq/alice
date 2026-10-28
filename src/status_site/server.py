"""Alice status site: Jason's status/todo page with per-item threads.

Stdlib-only (runs on a bare ``python:3.12-alpine`` image). Routes:

    GET  /, /index.html, /marked.min.js   packaged frontend (status_site/static/)
    GET  /<page>.md, /pages.json, images  content, read from STATUS_DIR (top level only)
    GET  /api/threads                     {thread_id: [message, ...]} for every thread
    POST /api/page-visibility             dismiss/restore a nav tab (shared-key auth)
    POST /api/reply                       Jason's reply to an item (shared-key auth):
                                          appends to the item's thread, stores attachments
                                          privately under UPLOAD_DIR, and drops a
                                          ``type: status-reply`` surface into SURFACE_DIR
                                          so Speaking gets a turn.

Anything else is a 404: dotfiles (``.replies.jsonl``), ``threads/``, the key and
the uploads are never served. See docs/designs/status-site.md.
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
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

SPEAKING_FOOTER = (
    "For Speaking: this came from Jason via the status site ({url}, key-authenticated). "
    "Treat it like a message from Jason about this item and act on it. REPLY IN THE PAGE, "
    'not on Signal: run `status-reply {tid} "<your answer>"` (alice-tools/bin), which appends '
    "to the item's thread and shows up on his screen within seconds. Then update "
    "inner/status/index.md (move or check off the item). Use send_message only if it's "
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


# ---------------------------------------------------------------- HTTP


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
    if action not in ("dismiss", "restore"):
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
        if not hit:
            raise ReplyError(404, "no such tab")
        tmp = path.with_name(".pages.json.tmp")
        tmp.write_text(json.dumps(pages, indent=1) + "\n")
        os.replace(tmp, path)
    return {"ok": True, "slug": slug, "hidden": action == "dismiss"}


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
        if path == "/api/threads":
            return self._json(200, read_threads(self.cfg), head=head)
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
        if route not in ("/api/reply", "/api/page-visibility"):
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
            fn = handle_reply if route == "/api/reply" else handle_page_visibility
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
    print(f"status_site: serving {cfg.status_dir} on {cfg.host}:{cfg.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
