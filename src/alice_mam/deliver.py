"""MAM → Kindle end-to-end delivery pipeline — Slice 2, step 2.

Once :mod:`alice_mam.acquire` has handed a .torrent to the seedbox,
the downstream chain is:

1. Seedbox downloads it to ``~/downloads/qbittorrent/<name>/``.
2. A cron on the media server (10.20.30.52) rclones completed
   downloads to ``/mnt/plex/downloads/seedbox/<name>/`` every 5 min
   with ``--min-age 5m`` (so partials don't get pulled).
3. :func:`wait_for_download` polls that dir for the matching subdir.
4. :func:`extract_epub` copies the first ``*.epub`` into
   ``/home/cronqj/media/flatbooks/`` (calibre-web's inbound dir).
5. :func:`calibre_add` runs ``calibredb add`` inside the calibre-web
   container via ssh → returns the book id.
6. :func:`send_to_kindle` triggers the SMTP send using calibre-web's
   Fernet-decrypted creds (procedure captured at
   ``cortex-memory/reference/2026-07-28-kindle-send-procedure.md``).

The :func:`main` CLI runs the whole pipeline. It is strict about
freeleech: if no freeleech candidate exists, it prints the top-3 and
exits non-zero WITHOUT consuming a download token.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from alice_mam import search as mam_search
from alice_mam.acquire import (
    download_torrent_bytes,
    load_cookie_from_secrets,
    send_to_seedbox_via_rclone,
)

# ─── Defaults ──────────────────────────────────────────────────────────

DEFAULT_SEEDBOX_DIR: Path = Path("/mnt/plex/downloads/seedbox")
DEFAULT_FLATBOOKS_DIR: Path = Path("/home/cronqj/media/flatbooks")
DEFAULT_CALIBRE_LIBRARY: str = "/books"
DEFAULT_CALIBRE_CONTAINER: str = "calibre-web"
DEFAULT_MEDIA_SSH: str = "cronqj@10.20.30.52"

# host-path → container-path prefix map for the calibre-web container.
# calibre-web runs with /home/cronqj/media/flatbooks mounted at /flatbooks.
HOST_TO_CONTAINER_FLATBOOKS: tuple[str, str] = (
    "/home/cronqj/media/flatbooks",
    "/flatbooks",
)


# ─── Download waiter ───────────────────────────────────────────────────


_TOKEN_SPLIT_RX = re.compile(r"[^A-Za-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_SPLIT_RX.split(text) if len(t) >= 2}


def _name_matches_hint(name: str, hint: str, *, min_overlap: int = 2) -> bool:
    """Token-overlap check — at least ``min_overlap`` meaningful tokens.

    We lowercase both and split on non-alphanumerics. This tolerates
    scene-style names like ``[Black.Library]_Twice.Dead.King-Ruin``
    matching a hint of ``Twice Dead King Ruin``.
    """
    name_tokens = _tokenize(name)
    hint_tokens = _tokenize(hint)
    if not hint_tokens:
        return False
    overlap = name_tokens & hint_tokens
    return len(overlap) >= min(min_overlap, len(hint_tokens))


def _lister_local(dir_path: Path) -> list[tuple[str, bool]]:
    """Return (name, is_dir) for each child of ``dir_path`` — local fs."""
    if not dir_path.exists():
        return []
    return [(c.name, c.is_dir()) for c in dir_path.iterdir()]


def _lister_over_ssh(
    ssh_host: str,
    runner: Callable[..., Any] = subprocess.run,
) -> Callable[[Path], list[tuple[str, bool]]]:
    """Factory: returns a lister that runs ``find`` on a remote host.

    Uses ``find <dir> -maxdepth 1 -mindepth 1 -printf '%y|%f\\n'`` so
    each line is ``d|<name>`` for dirs, ``f|<name>`` for regular files.
    """
    def lister(dir_path: Path) -> list[tuple[str, bool]]:
        remote = (
            f"test -d {str(dir_path)!r} && "
            f"find {str(dir_path)!r} -maxdepth 1 -mindepth 1 "
            r"-printf '%y|%f\n'"
        )
        result = runner(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                ssh_host, remote,
            ],
            capture_output=True, text=True, check=False,
        )
        out = (getattr(result, "stdout", "") or "").strip()
        if not out:
            return []
        rows: list[tuple[str, bool]] = []
        for line in out.splitlines():
            if "|" not in line:
                continue
            tag, _, name = line.partition("|")
            rows.append((name, tag == "d"))
        return rows

    return lister


def wait_for_download(
    torrent_name_hint: str,
    downloads_dir: Path = DEFAULT_SEEDBOX_DIR,
    timeout_minutes: int = 45,
    poll_interval_seconds: int = 30,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
    lister: Callable[[Path], list[tuple[str, bool]]] | None = None,
) -> Path:
    """Poll ``downloads_dir`` for an entry matching ``torrent_name_hint``.

    Returns the matching :class:`Path` — may be a subdirectory (common
    for multi-file torrents) or a single file (common for MAM epubs
    that ship as one ``.epub``).

    Raises :class:`TimeoutError` if nothing appears within
    ``timeout_minutes``. The rclone cron on the media server pulls
    completed downloads with ``--min-age 5m``, so a hit here implies
    the torrent is both finished on the seedbox AND fully copied
    locally (no partials).

    ``lister`` is injectable so callers can poll a remote host over
    SSH when ``downloads_dir`` isn't mounted locally (see
    :func:`_lister_over_ssh`).
    """
    downloads_dir = Path(downloads_dir)
    deadline = now() + timeout_minutes * 60.0
    say = log or (lambda _msg: None)
    list_dir = lister or _lister_local

    say(
        f"wait_for_download: polling {downloads_dir} for '{torrent_name_hint}' "
        f"(timeout={timeout_minutes}m, interval={poll_interval_seconds}s)"
    )

    while True:
        entries = list_dir(downloads_dir)
        # Prefer directory matches first (richer torrents with
        # cover.jpg + .epub + .nfo); fall back to loose files.
        dir_match: Path | None = None
        file_match: Path | None = None
        for name, is_dir in sorted(entries):
            if not _name_matches_hint(name, torrent_name_hint):
                continue
            child = downloads_dir / name
            if is_dir and dir_match is None:
                dir_match = child
            elif (not is_dir) and file_match is None:
                file_match = child
        match = dir_match or file_match
        if match is not None:
            say(f"wait_for_download: matched {match}")
            return match

        if now() >= deadline:
            raise TimeoutError(
                f"wait_for_download: no match for {torrent_name_hint!r} "
                f"under {downloads_dir} within {timeout_minutes} min"
            )
        sleep(poll_interval_seconds)


# ─── Epub extraction ───────────────────────────────────────────────────


def extract_epub(
    download_dir: Path,
    dest: Path = DEFAULT_FLATBOOKS_DIR,
    *,
    copyfile: Callable[[str, str], str] = shutil.copy2,
) -> Path:
    """Copy the first ``*.epub`` under ``download_dir`` to ``dest``.

    ``download_dir`` may also be a single ``.epub`` file (common for
    MAM torrents that are one epub per torrent — no containing dir
    on the seedbox). In that case we copy the file as-is.

    Returns the Path of the copied file in ``dest``.
    Raises :class:`FileNotFoundError` if no ``*.epub`` is present.
    """
    download_dir = Path(download_dir)
    dest = Path(dest)
    if not download_dir.exists():
        raise FileNotFoundError(f"download_dir does not exist: {download_dir}")

    if download_dir.is_file():
        if download_dir.suffix.lower() != ".epub":
            raise FileNotFoundError(
                f"download target is not a .epub file: {download_dir}"
            )
        matches = [download_dir]
    else:
        matches = sorted(download_dir.rglob("*.epub"))
    if not matches:
        raise FileNotFoundError(
            f"No .epub files under {download_dir} — nothing to send to Kindle"
        )
    src = matches[0]
    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / src.name
    copyfile(str(src), str(out_path))
    return out_path


def extract_epub_via_ssh(
    download_path: Path,
    ssh_host: str = DEFAULT_MEDIA_SSH,
    dest: Path = DEFAULT_FLATBOOKS_DIR,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Find + copy first .epub on a remote host — alice→media variant.

    Returns the remote dest-path (a :class:`Path`) where the .epub
    now lives. Used when the alice container can't see
    ``/mnt/plex/downloads/seedbox`` directly but can ssh to a host
    (media, 10.20.30.52) that can.

    Idempotent via ``cp -n``.
    """
    download_path = Path(download_path)
    dest = Path(dest)
    # One shell snippet: if the path is a directory, find first .epub
    # inside it; else treat the path itself as the source. Copy to dest
    # with cp -n (no-clobber); print final destination path on stdout.
    script = f'''set -e
SRC={str(download_path)!r}
DEST_DIR={str(dest)!r}
mkdir -p "$DEST_DIR"
if [ -d "$SRC" ]; then
    EPUB=$(find "$SRC" -maxdepth 4 -type f -name '*.epub' | sort | head -n1)
elif [ -f "$SRC" ] && case "$SRC" in *.epub|*.EPUB) true;; *) false;; esac; then
    EPUB="$SRC"
else
    echo "no .epub at $SRC" >&2
    exit 2
fi
if [ -z "$EPUB" ]; then
    echo "no .epub under $SRC" >&2
    exit 2
fi
BASENAME=$(basename "$EPUB")
cp -n "$EPUB" "$DEST_DIR/$BASENAME"
echo "$DEST_DIR/$BASENAME"
'''
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        ssh_host, "bash -s",
    ]
    result = runner(cmd, input=script, capture_output=True, text=True, check=False)
    rc = getattr(result, "returncode", 0)
    stdout = (getattr(result, "stdout", "") or "").strip()
    stderr = (getattr(result, "stderr", "") or "").strip()
    if rc != 0 or not stdout:
        raise FileNotFoundError(
            f"extract_epub_via_ssh: rc={rc} stderr={stderr[:300]!r}"
        )
    return Path(stdout.splitlines()[-1].strip())


# ─── Calibre add ───────────────────────────────────────────────────────


class CalibreAddError(RuntimeError):
    """Raised when calibredb add fails or cannot report a book id."""


_BOOK_ID_RX = re.compile(r"Added book ids?:\s*([0-9]+(?:\s*,\s*[0-9]+)*)", re.I)


def _parse_book_id(stdout: str) -> int:
    """Extract the book id from calibredb-add stdout.

    calibre reports ``Added book ids: <id>[, <id>...]`` — we take the
    first one (we add one book at a time).
    """
    m = _BOOK_ID_RX.search(stdout or "")
    if not m:
        raise CalibreAddError(
            f"Could not parse book id from calibredb output: {stdout[:400]}"
        )
    first = m.group(1).split(",")[0].strip()
    return int(first)


def _host_path_to_container(path: Path) -> str:
    """Translate /home/cronqj/media/flatbooks/<x> → /flatbooks/<x>."""
    s = str(path)
    host_prefix, container_prefix = HOST_TO_CONTAINER_FLATBOOKS
    if s.startswith(host_prefix):
        return container_prefix + s[len(host_prefix):]
    # If an unmapped path came in, fall back to the host path. The
    # container may or may not see it — calibredb will report file-not-
    # found, which bubbles up through :class:`CalibreAddError`.
    return s


def calibre_add(
    epub_path: Path,
    library: str = DEFAULT_CALIBRE_LIBRARY,
    container: str = DEFAULT_CALIBRE_CONTAINER,
    ssh_host: str = DEFAULT_MEDIA_SSH,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Run ``calibredb add`` in the calibre-web container via ssh.

    Returns the newly-added book id. Raises :class:`CalibreAddError`
    on non-zero exit OR on unparseable stdout.
    """
    epub_path = Path(epub_path)
    container_path = _host_path_to_container(epub_path)
    remote_cmd = (
        f"docker exec {container} calibredb add "
        f"--library-path={library} "
        f"'{container_path}'"
    )
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        ssh_host,
        remote_cmd,
    ]
    result = runner(cmd, capture_output=True, text=True, check=False)
    rc = getattr(result, "returncode", 0)
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    if rc != 0:
        raise CalibreAddError(
            f"calibredb add failed rc={rc} on {ssh_host}: "
            f"stderr={stderr.strip()[:400]!r} stdout={stdout.strip()[:400]!r}"
        )
    return _parse_book_id(stdout + "\n" + stderr)


# ─── Kindle send (SMTP via calibre-web creds) ─────────────────────────


class KindleSendError(RuntimeError):
    """Raised when the SMTP send fails or kindle_email can't be resolved."""


# The embedded script runs inside the calibre-web container (python3
# on PATH, cryptography already installed for calibre-web itself).
# It:
#   * Decrypts mail_password_e via Fernet with /config/.key
#   * Reads the book path from calibredb list for the given id
#   * Looks up the recipient from the user table if not provided
#   * Builds an EPUB EmailMessage and sends it via STARTTLS
#
# Kindle only accepts EPUB/PDF/DOC/RTF/HTM/images (vault note
# reference_kindle_personal_documents_accepted_formats). We force
# EPUB; if the book was added as a different format we surface an
# error rather than silently drop.
_SMTP_SCRIPT = r'''
import json, os, smtplib, sqlite3, sys
from email.message import EmailMessage
from cryptography.fernet import Fernet

book_id = int(os.environ["BOOK_ID"])
kindle_email_override = os.environ.get("KINDLE_EMAIL", "").strip() or None

with open("/config/.key", "rb") as fh:
    key = fh.read()
fernet = Fernet(key)
db = sqlite3.connect("/config/app.db")
row = db.execute(
    "SELECT mail_server, mail_port, mail_login, mail_password_e, mail_from, mail_use_ssl "
    "FROM settings LIMIT 1"
).fetchone()
if not row:
    print(json.dumps({"ok": False, "stage": "settings", "err": "no settings row"}))
    sys.exit(2)
server, port, login, pw_enc, mail_from, use_ssl = row
password = fernet.decrypt(pw_enc).decode()

# Find the book file via calibredb list for id
import subprocess
list_cmd = [
    "calibredb", "list",
    "--library-path=/books",
    "--fields=id,formats,title",
    "--for-machine",
    "--search", f"id:{book_id}",
]
res = subprocess.run(list_cmd, capture_output=True, text=True)
if res.returncode != 0:
    print(json.dumps({"ok": False, "stage": "calibredb_list",
                      "err": res.stderr[:500], "stdout": res.stdout[:500]}))
    sys.exit(3)
books = json.loads(res.stdout or "[]")
if not books:
    print(json.dumps({"ok": False, "stage": "calibredb_list",
                      "err": f"no book with id {book_id}"}))
    sys.exit(3)
book = books[0]
formats = book.get("formats") or []
epub_path = next((p for p in formats if p.lower().endswith(".epub")), None)
if not epub_path:
    print(json.dumps({"ok": False, "stage": "format_check",
                      "err": f"no EPUB format for id {book_id}; formats={formats}"}))
    sys.exit(4)

# Resolve recipient
if kindle_email_override:
    kindle_email = kindle_email_override
else:
    # calibre-web stores a per-user kindle_mail on the user table.
    try:
        urow = db.execute(
            "SELECT kindle_mail FROM user WHERE kindle_mail IS NOT NULL "
            "AND kindle_mail != '' LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        urow = None
    if not urow or not urow[0]:
        print(json.dumps({"ok": False, "stage": "resolve_kindle_email",
                          "err": "no kindle_mail in calibre-web user table"}))
        sys.exit(5)
    kindle_email = urow[0]

title = book.get("title") or f"Book {book_id}"
with open(epub_path, "rb") as fh:
    epub_bytes = fh.read()

msg = EmailMessage()
msg["Subject"] = title
msg["From"] = mail_from or login
msg["To"] = kindle_email
msg.set_content(f"Sent from the MAM delivery pipeline: {title}")
msg.add_attachment(
    epub_bytes,
    maintype="application",
    subtype="epub+zip",
    filename=os.path.basename(epub_path),
)

try:
    with smtplib.SMTP(server, int(port), timeout=60) as s:
        s.ehlo()
        if int(use_ssl or 0) != 2:  # 2 = SSL/TLS wrap mode; 1 = STARTTLS
            s.starttls()
            s.ehlo()
        s.login(login, password)
        s.send_message(msg)
except Exception as exc:
    print(json.dumps({"ok": False, "stage": "smtp",
                      "err": f"{type(exc).__name__}: {exc}",
                      "server": server, "port": port, "to": kindle_email}))
    sys.exit(6)

print(json.dumps({"ok": True, "kindle_email": kindle_email,
                  "server": server, "port": port, "title": title,
                  "epub_path": epub_path}))
'''


def send_to_kindle(
    book_id: int,
    ssh_host: str = DEFAULT_MEDIA_SSH,
    kindle_email: str | None = None,
    *,
    container: str = DEFAULT_CALIBRE_CONTAINER,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Send the book identified by ``book_id`` via calibre-web's SMTP creds.

    Returns a dict ``{"ok": True, "kindle_email": ..., ...}`` on
    success. Raises :class:`KindleSendError` on any failure, with the
    stage + error message surfaced.

    If ``kindle_email`` is None, the embedded script pulls it from the
    calibre-web user table.
    """
    if book_id <= 0:
        raise ValueError(f"book_id must be positive, got {book_id}")
    env_prefix = f"BOOK_ID={int(book_id)} "
    if kindle_email:
        env_prefix += f"KINDLE_EMAIL={kindle_email} "
    remote_cmd = (
        f"{env_prefix}docker exec -i -e BOOK_ID -e KINDLE_EMAIL "
        f"{container} python3 -"
    )
    # The env_prefix above sets the vars in the ssh-remote shell;
    # `docker exec -e BOOK_ID -e KINDLE_EMAIL` forwards them into the
    # container, then python3 reads them from os.environ.
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        ssh_host,
        remote_cmd,
    ]
    result = runner(cmd, input=_SMTP_SCRIPT, capture_output=True, text=True, check=False)
    rc = getattr(result, "returncode", 0)
    stdout = (getattr(result, "stdout", "") or "").strip()
    stderr = (getattr(result, "stderr", "") or "").strip()

    # The embedded script's final line should be a JSON blob. Peel it
    # off the stdout tail; calibre tooling sometimes leaks warnings
    # above the JSON.
    parsed: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if parsed is None:
        raise KindleSendError(
            f"send_to_kindle: no JSON status from remote (rc={rc}). "
            f"stdout={stdout[:500]!r} stderr={stderr[:500]!r}"
        )
    if not parsed.get("ok"):
        raise KindleSendError(
            f"send_to_kindle failed at stage={parsed.get('stage')}: "
            f"{parsed.get('err')} (stderr={stderr[:200]!r})"
        )
    if rc != 0:
        # Script succeeded but ssh/docker returned non-zero — surface it.
        raise KindleSendError(
            f"send_to_kindle: remote returned rc={rc} despite ok status: "
            f"stderr={stderr[:200]!r}"
        )
    return parsed


# ─── CLI ──────────────────────────────────────────────────────────────


def _format_candidate_for_surface(c: mam_search.Candidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "authors": c.authors,
        "seeders": c.seeders,
        "size_bytes": c.size_bytes,
        "size_mib": round(c.size_bytes / (1024 * 1024), 1),
        "filetypes": c.filetypes,
        "free": c.free,
        "personal_freeleech": c.personal_freeleech,
    }


def _run_pipeline(
    title: str,
    author: str,
    *,
    cookie: str | None = None,
    freeleech_only: bool = True,
    kindle_email: str | None = None,
    logger: Callable[[str], None] = print,
    downloads_dir: Path = DEFAULT_SEEDBOX_DIR,
    flatbooks_dir: Path = DEFAULT_FLATBOOKS_DIR,
    timeout_minutes: int = 45,
    poll_interval_seconds: int = 30,
    via_ssh_host: str | None = None,
) -> dict[str, Any]:
    """End-to-end driver, suitable for CLI + future Signal command path.

    Returns a status dict either way; `status["delivered"]` says whether
    a Kindle email actually went out. Raises on unrecoverable errors.
    """
    query = f"{title} {author}".strip()
    logger(f"[1/6] search: {query!r}")
    if cookie is None:
        try:
            cookie = load_cookie_from_secrets()
        except (FileNotFoundError, ValueError) as exc:
            logger(f"cookie load failed: {exc}")
            raise

    candidates = mam_search.search_mam(query, cookie=cookie)
    logger(f"       → {len(candidates)} candidates")
    if not candidates:
        return {"delivered": False, "reason": "no_candidates", "top3": []}

    logger("[2/6] pick candidate (freeleech-only=%s)" % freeleech_only)
    chosen = mam_search.pick_candidate(candidates, require_freeleech=freeleech_only)
    if chosen is None:
        top3 = [_format_candidate_for_surface(c) for c in candidates[:3]]
        logger("       → NO FREELEECH HIT — surfacing top-3 to Jason, exiting")
        return {
            "delivered": False,
            "reason": "no_freeleech",
            "top3": top3,
        }
    logger(f"       → picked id={chosen.id} title={chosen.title!r} free={chosen.free}")

    logger("[3/6] fetch .torrent bytes")
    tbytes = download_torrent_bytes(chosen.download_url, cookie=cookie)
    logger(f"       → {len(tbytes)} bytes")

    logger("[4/6] hand off .torrent to seedbox watch folder (via media rclone)")
    hint_stem = (title.replace(" ", "-").lower() or "alice-mam")[:48]
    dest = send_to_seedbox_via_rclone(tbytes, name_hint=hint_stem)
    logger(f"       → {dest}")

    hint = f"{title} {author}"
    if via_ssh_host:
        lister = _lister_over_ssh(via_ssh_host)
        logger(
            f"[5/6] wait for download to land under {downloads_dir} "
            f"(polling via ssh {via_ssh_host})"
        )
    else:
        lister = None
        logger(f"[5/6] wait for download to land under {downloads_dir}")
    download_dir = wait_for_download(
        hint,
        downloads_dir=downloads_dir,
        timeout_minutes=timeout_minutes,
        poll_interval_seconds=poll_interval_seconds,
        log=logger,
        lister=lister,
    )
    logger(f"       → {download_dir}")

    logger("[6/6] extract epub → calibre_add → send_to_kindle")
    if via_ssh_host:
        epub = extract_epub_via_ssh(
            download_dir, ssh_host=via_ssh_host, dest=flatbooks_dir
        )
    else:
        epub = extract_epub(download_dir, dest=flatbooks_dir)
    logger(f"       extract: {epub}")
    book_id = calibre_add(epub)
    logger(f"       calibre_add: book_id={book_id}")
    smtp_status = send_to_kindle(book_id, kindle_email=kindle_email)
    logger(
        f"       send_to_kindle: ok, to={smtp_status.get('kindle_email')} "
        f"via {smtp_status.get('server')}:{smtp_status.get('port')}"
    )

    return {
        "delivered": True,
        "book_id": book_id,
        "kindle_email": smtp_status.get("kindle_email"),
        "epub_path": smtp_status.get("epub_path"),
        "chosen": _format_candidate_for_surface(chosen),
    }


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m alice_mam.deliver",
        description=(
            "Search MAM for <title> <author> and deliver the best "
            "freeleech match to Jason's Kindle, end-to-end."
        ),
    )
    p.add_argument("title")
    p.add_argument("author")
    p.add_argument(
        "--allow-non-freeleech",
        action="store_true",
        help=(
            "DANGEROUS: allow spending a download token. Default is "
            "freeleech-only; if no freeleech hit, we surface and exit."
        ),
    )
    p.add_argument(
        "--kindle-email",
        default=None,
        help="Override Kindle recipient (otherwise pulled from calibre-web).",
    )
    p.add_argument(
        "--downloads-dir",
        default=str(DEFAULT_SEEDBOX_DIR),
        help="Where to poll for the rclone-landed download.",
    )
    p.add_argument(
        "--flatbooks-dir",
        default=str(DEFAULT_FLATBOOKS_DIR),
        help="Where to drop the extracted epub for calibre-web.",
    )
    p.add_argument(
        "--timeout-minutes",
        type=int,
        default=45,
        help="Max wait for the download to land.",
    )
    p.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=30,
        help="Polling cadence for the download-landed check.",
    )
    p.add_argument(
        "--via-ssh-host",
        default=DEFAULT_MEDIA_SSH,
        help=(
            "SSH host used to poll ``downloads-dir`` + copy the .epub "
            "into flatbooks. The daemon container doesn't mount "
            "/mnt/plex, so by default we drive step 5+ over ssh to "
            "media. Pass '' to run the local filesystem path instead."
        ),
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    parser = _build_argparser()
    ns = parser.parse_args(list(argv) if argv is not None else None)

    def say(msg: str) -> None:
        print(f"[alice-mam.deliver] {msg}", flush=True)

    try:
        status = _run_pipeline(
            ns.title,
            ns.author,
            freeleech_only=not ns.allow_non_freeleech,
            kindle_email=ns.kindle_email,
            logger=say,
            downloads_dir=Path(ns.downloads_dir),
            flatbooks_dir=Path(ns.flatbooks_dir),
            timeout_minutes=ns.timeout_minutes,
            poll_interval_seconds=ns.poll_interval_seconds,
            via_ssh_host=ns.via_ssh_host or None,
        )
    except Exception as exc:
        say(f"ERROR: {type(exc).__name__}: {exc}")
        raise

    print(json.dumps(status, indent=2))
    if not status.get("delivered"):
        if status.get("reason") == "no_freeleech":
            return 1
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "CalibreAddError",
    "KindleSendError",
    "calibre_add",
    "extract_epub",
    "extract_epub_via_ssh",
    "main",
    "send_to_kindle",
    "wait_for_download",
]
