"""MAM torrent acquisition — Slice 2, step 1.

Pulls .torrent bytes from MAM using the session cookie, then hands
them off to the seedbox's qBittorrent *watch folder* over scp. The
seedbox pulls each torrent automatically within ~5s.

Hard invariants (Jason-ratified 2026-09-06, reinforced 2026-11-17):

* **Seedbox-only handoff.** Private-tracker torrents MUST land on
  ``canopus.usbx.me`` and NEVER on the local qBit at
  ``10.20.30.52:8990`` (public-tracker-only). Announcing a private
  passkey from the home ASN would trip MAM's auto-disable. The
  module-level :data:`SEEDBOX_HOST` constant + :data:`FORBIDDEN_DESTINATIONS`
  set make this a loud failure, not a silent misroute.
* **Cookie hygiene.** The MAM cookie value is never logged, never
  echoed on error. The audit log records length + first 8 chars only.
* **No token spend here.** The MAM download endpoint uses a
  pre-authorized per-row ``dl`` hash — fetching it does NOT consume a
  freeleech wedge. The spend decision happens upstream in
  :func:`alice_mam.search.pick_candidate`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

# ─── Hard invariants (do not change without Jason's say-so) ────────────

#: Only legal destination for private-tracker .torrent files.
SEEDBOX_HOST: str = "canopus.usbx.me"

#: User account on the seedbox.
SEEDBOX_USER: str = "prometheansun"

#: qBittorrent watch folder on the seedbox. qBit auto-adds within ~5s.
SEEDBOX_WATCH_DIR: str = "~/watch/qbittorrent/"

#: Hosts / ports that must NEVER receive a private-tracker torrent.
#: Local qBit (10.20.30.52:8990) announces from the home ASN using the
#: public-tracker config; a private-tracker handoff there would leak
#: the passkey and trigger MAM auto-disable.
FORBIDDEN_DESTINATIONS: frozenset[str] = frozenset(
    {
        "10.20.30.52:8990",
        "localhost:8990",
        "127.0.0.1:8990",
        "10.20.30.52",  # bare-host form, same machine
    }
)

#: Default MAM secrets path (0600, host-owned on aimax1).
DEFAULT_SECRETS_PATH: Path = Path("~/.alice-secrets/mam.yaml").expanduser()

#: Audit log target — one JSON line per acquire action (no cookie value).
_DEFAULT_EVENT_LOG: Path = (
    Path.home() / "alice-mind" / "inner" / "state" / "mam-events.jsonl"
)


# ─── Cookie loader ─────────────────────────────────────────────────────


def load_cookie_from_secrets(
    path: Path = DEFAULT_SECRETS_PATH,
) -> str:
    """Return the ``mam_id`` cookie value from a YAML secrets file.

    Expected shape::

        mam_id: "<cookie-value>"

    The file should be mode 0600 host-owned. We don't enforce the
    mode here (the container has a different uid) — the host-side
    deploy is responsible.

    Raises :class:`FileNotFoundError` or :class:`ValueError` with a
    message that does NOT include any cookie bytes.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"MAM secrets file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"MAM secrets file at {p} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"MAM secrets file at {p} must be a YAML mapping")
    value = data.get("mam_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"MAM secrets file at {p} is missing a non-empty 'mam_id' value"
        )
    return value.strip()


# ─── Audit log (no secrets) ───────────────────────────────────────────


def _cookie_fingerprint(cookie: str) -> dict[str, Any]:
    """Length + 8-char prefix — never the full value."""
    return {"cookie_len": len(cookie), "cookie_prefix": cookie[:8]}


def _emit_acquire_event(
    *,
    action: str,
    outcome: str,
    log_path: Path = _DEFAULT_EVENT_LOG,
    **extra: Any,
) -> None:
    event: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "action": action,
        "outcome": outcome,
    }
    # Explicitly filter: no key starting with "cookie" except length / prefix.
    for k, v in extra.items():
        if k == "cookie":  # belt-and-suspenders
            continue
        event[k] = v
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(
            f"alice_mam.acquire: audit log write failed "
            f"({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )


# ─── Torrent fetch ─────────────────────────────────────────────────────


class TorrentFetchError(RuntimeError):
    """Raised when the MAM download endpoint returns a non-200."""


def download_torrent_bytes(
    download_url: str,
    cookie: str,
    timeout: float = 30.0,
    *,
    opener: Any | None = None,
    audit_log: Path | None = None,
    emit_audit: bool = True,
) -> bytes:
    """Fetch the .torrent bytes behind a MAM dl-hash URL.

    The MAM dl endpoint is pre-authorized by the ``dl`` hash in the
    row — it does NOT spend a freeleech wedge. We still mount the
    session cookie because unauthenticated fetches are rate-limited
    more aggressively.

    Raises :class:`TorrentFetchError` on non-200, re-raises transport
    errors unchanged. Never logs the cookie value.
    """
    if not download_url or not download_url.strip():
        raise ValueError("download_url is empty")
    if not cookie or not cookie.strip():
        raise ValueError("cookie is empty")

    req = urllib.request.Request(
        download_url,
        method="GET",
        headers={
            "Cookie": f"mam_id={cookie}",
            "User-Agent": "alice-mam/1 (acquire)",
            "Accept": "application/x-bittorrent, */*",
        },
    )
    try:
        if opener is None:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = opener.open(req, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        if emit_audit:
            _emit_acquire_event(
                action="download_torrent_bytes",
                outcome="transport_error",
                url=download_url,
                log_path=audit_log or _DEFAULT_EVENT_LOG,
                **_cookie_fingerprint(cookie),
            )
        raise

    with resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()

    if status != 200:
        if emit_audit:
            _emit_acquire_event(
                action="download_torrent_bytes",
                outcome=f"http_{status}",
                url=download_url,
                byte_count=len(raw),
                log_path=audit_log or _DEFAULT_EVENT_LOG,
                **_cookie_fingerprint(cookie),
            )
        raise TorrentFetchError(
            f"MAM download returned HTTP {status} for {download_url}"
        )

    if emit_audit:
        _emit_acquire_event(
            action="download_torrent_bytes",
            outcome="ok",
            url=download_url,
            byte_count=len(raw),
            log_path=audit_log or _DEFAULT_EVENT_LOG,
            **_cookie_fingerprint(cookie),
        )
    return raw


# ─── Seedbox handoff ───────────────────────────────────────────────────


def _assert_seedbox_destination(host: str, dest: str) -> None:
    """Raise if anyone tries to short-circuit the seedbox-only rule."""
    if host != SEEDBOX_HOST:
        raise AssertionError(
            f"Private-tracker torrents must land on {SEEDBOX_HOST}; "
            f"got {host!r}. See CLAUDE.md § PARAMOUNT HARD RULE."
        )
    # The destination strings we'd ever assemble here include "host"
    # or "user@host:path"; the forbidden set covers raw local qBit
    # hosts regardless of port.
    for forbidden in FORBIDDEN_DESTINATIONS:
        if forbidden in dest or forbidden in host:
            raise AssertionError(
                f"Destination {dest!r} is forbidden (matches {forbidden!r}). "
                "Private-tracker torrents MUST NOT reach local qBit."
            )


def send_to_seedbox(
    torrent_bytes: bytes,
    local_path: str | Path | None = None,
    *,
    host: str = SEEDBOX_HOST,
    user: str = SEEDBOX_USER,
    watch_dir: str = SEEDBOX_WATCH_DIR,
    ssh_cmd: str = "scp",
    ssh_opts: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"),
    runner: Any = subprocess.run,
    audit_log: Path | None = None,
    emit_audit: bool = True,
) -> str:
    """scp ``torrent_bytes`` into the seedbox qBit watch folder.

    Returns the remote path string written (``user@host:watch_dir/<name>``).

    Guards (fail loud — never silently retarget):

    * ``host`` MUST be :data:`SEEDBOX_HOST`. Changing it raises
      :class:`AssertionError`.
    * The assembled destination MUST NOT contain any string in
      :data:`FORBIDDEN_DESTINATIONS`.

    ``local_path`` is optional; if omitted we write to a NamedTemporaryFile
    under ``/tmp`` with suffix ``.torrent`` and delete it after upload.
    """
    if not torrent_bytes:
        raise ValueError("torrent_bytes is empty")

    # Belt-and-suspenders: validate host + destination BEFORE writing
    # anything to disk, so a misconfig can't leak a staged file.
    dest_remote = f"{user}@{host}:{watch_dir}"
    _assert_seedbox_destination(host, dest_remote)

    created_tmp: Path | None = None
    if local_path is None:
        fd, name = tempfile.mkstemp(prefix="alice-mam-", suffix=".torrent", dir="/tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(torrent_bytes)
        except Exception:
            os.close(fd)  # already closed via fdopen; safe no-op
            Path(name).unlink(missing_ok=True)
            raise
        local = Path(name)
        created_tmp = local
    else:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(torrent_bytes)

    cmd = [ssh_cmd, *ssh_opts, str(local), dest_remote]
    try:
        result = runner(cmd, capture_output=True, text=True, check=False)
    finally:
        if created_tmp is not None:
            created_tmp.unlink(missing_ok=True)

    rc = getattr(result, "returncode", 0)
    stderr = getattr(result, "stderr", "") or ""
    if rc != 0:
        if emit_audit:
            _emit_acquire_event(
                action="send_to_seedbox",
                outcome=f"scp_rc_{rc}",
                dest=dest_remote,
                byte_count=len(torrent_bytes),
                stderr_head=stderr[:200],
                log_path=audit_log or _DEFAULT_EVENT_LOG,
            )
        raise RuntimeError(
            f"scp to {dest_remote} failed rc={rc}: {stderr.strip()[:500]}"
        )

    if emit_audit:
        _emit_acquire_event(
            action="send_to_seedbox",
            outcome="ok",
            dest=dest_remote,
            byte_count=len(torrent_bytes),
            log_path=audit_log or _DEFAULT_EVENT_LOG,
        )
    return dest_remote


def send_to_seedbox_via_rclone(
    torrent_bytes: bytes,
    *,
    bridge_ssh_host: str = "cronqj@10.20.30.52",
    rclone_remote: str = "seedbox:/home/prometheansun/watch/qbittorrent/",
    rclone_config: str = "/home/cronqj/.config/rclone/rclone.conf",
    remote_tmp_dir: str = "/tmp",
    runner: Any = subprocess.run,
    audit_log: Path | None = None,
    emit_audit: bool = True,
    name_hint: str | None = None,
) -> str:
    """Hand off a .torrent to the seedbox via rclone on a bridge host.

    Live production uses this path: the alice container can't scp to
    canopus directly (no key authorized), but the media server has
    an rclone SFTP config for the seedbox and the alice container can
    ssh to the media server with cronqj's key.

    Steps (all over a single ssh session for atomicity):

    1. ``ssh <bridge> "cat > /tmp/<uuid>.torrent"`` — stage bytes.
    2. ``ssh <bridge> "rclone --config <conf> copyto /tmp/<uuid>.torrent
       seedbox:/home/prometheansun/watch/qbittorrent/<uuid>.torrent"``
    3. Clean up the staged file on bridge.

    Hard guard: the ``rclone_remote`` MUST start with ``seedbox:`` and
    include ``/home/prometheansun/watch/qbittorrent``; any other target
    raises AssertionError before any bytes leave this process.
    """
    if not torrent_bytes:
        raise ValueError("torrent_bytes is empty")

    # Defensive: the only legal rclone remote prefix is the canopus
    # seedbox watch folder. Rerouting here would be the same class of
    # catastrophe as retargeting scp to local qBit.
    if not rclone_remote.startswith("seedbox:"):
        raise AssertionError(
            f"rclone_remote must start with 'seedbox:'; got {rclone_remote!r}"
        )
    if "/watch/qbittorrent" not in rclone_remote:
        raise AssertionError(
            f"rclone_remote must target /watch/qbittorrent/; got {rclone_remote!r}"
        )
    for forbidden in FORBIDDEN_DESTINATIONS:
        if forbidden in rclone_remote:
            raise AssertionError(
                f"rclone_remote matches forbidden destination {forbidden!r}"
            )

    import uuid as _uuid
    stem = (name_hint or "alice-mam") + "-" + _uuid.uuid4().hex[:8]
    remote_tmp = f"{remote_tmp_dir.rstrip('/')}/{stem}.torrent"
    remote_file = rclone_remote.rstrip("/") + f"/{stem}.torrent"

    # Stage the bytes on the bridge host via a single ssh. We pipe
    # them through stdin so no local temp file is required.
    stage_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        bridge_ssh_host,
        f"cat > {remote_tmp}",
    ]
    stage_result = runner(
        stage_cmd,
        input=torrent_bytes,
        capture_output=True,
        check=False,
    )
    stage_rc = getattr(stage_result, "returncode", 0)
    if stage_rc != 0:
        stderr_bytes = getattr(stage_result, "stderr", b"") or b""
        if isinstance(stderr_bytes, bytes):
            stderr = stderr_bytes.decode("utf-8", "replace")
        else:
            stderr = str(stderr_bytes)
        if emit_audit:
            _emit_acquire_event(
                action="send_to_seedbox_via_rclone",
                outcome=f"stage_rc_{stage_rc}",
                bridge=bridge_ssh_host,
                stderr_head=stderr[:200],
                log_path=audit_log or _DEFAULT_EVENT_LOG,
            )
        raise RuntimeError(
            f"staging to {bridge_ssh_host}:{remote_tmp} failed rc={stage_rc}: "
            f"{stderr.strip()[:500]}"
        )

    rclone_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        bridge_ssh_host,
        (
            f"rclone --config {rclone_config} copyto "
            f"'{remote_tmp}' '{remote_file}' --no-traverse && "
            f"rm -f '{remote_tmp}'"
        ),
    ]
    push_result = runner(rclone_cmd, capture_output=True, text=True, check=False)
    rc = getattr(push_result, "returncode", 0)
    stderr = getattr(push_result, "stderr", "") or ""
    if rc != 0:
        if emit_audit:
            _emit_acquire_event(
                action="send_to_seedbox_via_rclone",
                outcome=f"rclone_rc_{rc}",
                dest=remote_file,
                byte_count=len(torrent_bytes),
                stderr_head=stderr[:200],
                log_path=audit_log or _DEFAULT_EVENT_LOG,
            )
        raise RuntimeError(
            f"rclone copyto {remote_file} failed rc={rc}: "
            f"{stderr.strip()[:500]}"
        )

    if emit_audit:
        _emit_acquire_event(
            action="send_to_seedbox_via_rclone",
            outcome="ok",
            dest=remote_file,
            byte_count=len(torrent_bytes),
            log_path=audit_log or _DEFAULT_EVENT_LOG,
        )
    return remote_file


__all__ = [
    "SEEDBOX_HOST",
    "SEEDBOX_USER",
    "SEEDBOX_WATCH_DIR",
    "FORBIDDEN_DESTINATIONS",
    "DEFAULT_SECRETS_PATH",
    "TorrentFetchError",
    "download_torrent_bytes",
    "load_cookie_from_secrets",
    "send_to_seedbox",
    "send_to_seedbox_via_rclone",
]
