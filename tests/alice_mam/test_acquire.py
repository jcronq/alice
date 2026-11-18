"""Tests for :mod:`alice_mam.acquire` — torrent fetch + seedbox handoff.

Live-network calls MUST NOT happen in the suite. The ``_FakeOpener``
mirrors the shape used by :mod:`tests.alice_mam.test_search` and the
seedbox handoff uses an injected ``runner`` so we never shell out.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from alice_mam.acquire import (
    FORBIDDEN_DESTINATIONS,
    SEEDBOX_HOST,
    SEEDBOX_USER,
    SEEDBOX_WATCH_DIR,
    TorrentFetchError,
    _assert_seedbox_destination,
    download_torrent_bytes,
    load_cookie_from_secrets,
    send_to_seedbox,
    send_to_seedbox_via_rclone,
)


# ─── HTTP stubs (same shape as slice 1) ────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class _FakeOpener:
    def __init__(self, resp=None, exc=None, capture=None):
        self._resp = resp
        self._exc = exc
        self._capture = capture

    def open(self, req, timeout=None):
        if self._capture is not None:
            self._capture["url"] = req.full_url
            self._capture["headers"] = dict(req.header_items())
            self._capture["method"] = req.get_method()
            self._capture["timeout"] = timeout
        if self._exc is not None:
            raise self._exc
        return self._resp


# ─── load_cookie_from_secrets ──────────────────────────────────────────


def test_load_cookie_from_secrets_parses_yaml(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text('mam_id: "abc123cookie-value"\n')
    assert load_cookie_from_secrets(p) == "abc123cookie-value"


def test_load_cookie_from_secrets_strips_whitespace(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text('mam_id: "  padded  "\n')
    assert load_cookie_from_secrets(p) == "padded"


def test_load_cookie_from_secrets_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cookie_from_secrets(tmp_path / "nowhere.yaml")


def test_load_cookie_from_secrets_rejects_missing_key(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text("other_field: 123\n")
    with pytest.raises(ValueError, match="mam_id"):
        load_cookie_from_secrets(p)


def test_load_cookie_from_secrets_rejects_empty_value(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text('mam_id: ""\n')
    with pytest.raises(ValueError, match="mam_id"):
        load_cookie_from_secrets(p)


def test_load_cookie_from_secrets_rejects_non_mapping(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text("- just-a-list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_cookie_from_secrets(p)


def test_load_cookie_from_secrets_rejects_invalid_yaml(tmp_path):
    p = tmp_path / "mam.yaml"
    p.write_text("mam_id: [unterminated\n")
    with pytest.raises(ValueError):
        load_cookie_from_secrets(p)


def test_load_cookie_error_message_does_not_leak_cookie(tmp_path):
    """Error strings must not echo the cookie bytes."""
    p = tmp_path / "mam.yaml"
    p.write_text("not_mam_id: super-secret-cookie-value-xyz\n")
    with pytest.raises(ValueError) as exc:
        load_cookie_from_secrets(p)
    assert "super-secret-cookie-value-xyz" not in str(exc.value)


# ─── download_torrent_bytes ────────────────────────────────────────────


def test_download_torrent_bytes_returns_bytes(tmp_path):
    body = b"d8:announce..."
    op = _FakeOpener(resp=_FakeResp(200, body))
    audit = tmp_path / "events.jsonl"
    out = download_torrent_bytes(
        "https://www.myanonamouse.net/tor/download.php/HASH?tid=42",
        cookie="fake-cookie",
        opener=op,
        audit_log=audit,
    )
    assert out == body
    # Audit log written and does not contain the full cookie.
    line = audit.read_text().strip()
    event = json.loads(line)
    assert event["action"] == "download_torrent_bytes"
    assert event["outcome"] == "ok"
    assert event["byte_count"] == len(body)
    assert event["cookie_len"] == len("fake-cookie")
    assert event["cookie_prefix"] == "fake-coo"
    # Explicit: the full cookie value must not appear anywhere in audit text.
    assert "fake-cookie" not in line
    # (cookie_prefix is 8 chars, so the full 11-char token can't match.)


def test_download_torrent_bytes_sends_cookie_header():
    cap: dict = {}
    op = _FakeOpener(resp=_FakeResp(200, b"ok"), capture=cap)
    download_torrent_bytes(
        "https://www.myanonamouse.net/tor/download.php/HASH?tid=1",
        cookie="xyz-cookie",
        opener=op,
        emit_audit=False,
    )
    headers = {k.lower(): v for k, v in cap["headers"].items()}
    assert headers["cookie"] == "mam_id=xyz-cookie"
    assert cap["method"] == "GET"


def test_download_torrent_bytes_raises_on_non_200(tmp_path):
    op = _FakeOpener(resp=_FakeResp(403, b"denied"))
    audit = tmp_path / "events.jsonl"
    with pytest.raises(TorrentFetchError):
        download_torrent_bytes(
            "https://www.myanonamouse.net/tor/download.php/HASH?tid=1",
            cookie="fake-cookie",
            opener=op,
            audit_log=audit,
        )
    # Audit log should still record the failure.
    event = json.loads(audit.read_text().strip())
    assert event["outcome"] == "http_403"


def test_download_torrent_bytes_reraises_transport_error():
    op = _FakeOpener(exc=urllib.error.URLError("dns boom"))
    with pytest.raises(urllib.error.URLError):
        download_torrent_bytes(
            "https://www.myanonamouse.net/tor/download.php/HASH?tid=1",
            cookie="fake-cookie",
            opener=op,
            emit_audit=False,
        )


def test_download_torrent_bytes_rejects_empty_args():
    with pytest.raises(ValueError):
        download_torrent_bytes("", cookie="x", emit_audit=False)
    with pytest.raises(ValueError):
        download_torrent_bytes("https://x/", cookie="", emit_audit=False)


# ─── Seedbox invariants ────────────────────────────────────────────────


def test_seedbox_host_constant_locked_in():
    """If someone edits this constant, the test name points right at it."""
    assert SEEDBOX_HOST == "canopus.usbx.me"
    assert SEEDBOX_USER == "prometheansun"
    assert SEEDBOX_WATCH_DIR == "~/watch/qbittorrent/"


def test_forbidden_destinations_lists_local_qbit():
    assert "10.20.30.52:8990" in FORBIDDEN_DESTINATIONS
    assert "localhost:8990" in FORBIDDEN_DESTINATIONS


def test_assert_seedbox_destination_accepts_canopus():
    # Correct destination: does not raise.
    _assert_seedbox_destination(
        host="canopus.usbx.me",
        dest="prometheansun@canopus.usbx.me:~/watch/qbittorrent/",
    )


@pytest.mark.parametrize(
    "host",
    [
        "10.20.30.52",
        "10.20.30.52:8990",
        "localhost",
        "127.0.0.1",
        "wrong.host.example",
    ],
)
def test_assert_seedbox_destination_rejects_non_canopus_hosts(host):
    with pytest.raises(AssertionError):
        _assert_seedbox_destination(
            host=host,
            dest=f"someuser@{host}:/tmp/",
        )


@pytest.mark.parametrize(
    "dest",
    [
        "someone@10.20.30.52:/downloads/",
        "qbit@10.20.30.52:8990/api/v2/torrents/add",
        "localhost:8990/add",
    ],
)
def test_assert_seedbox_destination_rejects_forbidden_dest_strings(dest):
    # Even if host is canopus, a forbidden string in the dest kills it.
    with pytest.raises(AssertionError):
        _assert_seedbox_destination(host=SEEDBOX_HOST, dest=dest)


# ─── send_to_seedbox (injected runner, no real scp) ────────────────────


class _FakeRunResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _recording_runner(records: list[dict], *, rc: int = 0, stderr: str = ""):
    def runner(cmd, **kwargs):
        records.append({"cmd": list(cmd), "kwargs": kwargs})
        return _FakeRunResult(returncode=rc, stderr=stderr)

    return runner


def test_send_to_seedbox_scps_to_canopus(tmp_path):
    records: list[dict] = []
    audit = tmp_path / "events.jsonl"
    dest = send_to_seedbox(
        b"d8:announce...",
        host=SEEDBOX_HOST,
        user=SEEDBOX_USER,
        watch_dir=SEEDBOX_WATCH_DIR,
        runner=_recording_runner(records),
        audit_log=audit,
    )
    assert dest == f"{SEEDBOX_USER}@{SEEDBOX_HOST}:{SEEDBOX_WATCH_DIR}"
    # Exactly one scp call recorded.
    assert len(records) == 1
    cmd = records[0]["cmd"]
    assert cmd[0] == "scp"
    assert cmd[-1] == dest
    # Audit log written.
    event = json.loads(audit.read_text().strip())
    assert event["action"] == "send_to_seedbox"
    assert event["outcome"] == "ok"
    assert event["dest"] == dest


def test_send_to_seedbox_rejects_local_qbit_host():
    """The paramount hard rule: never reroute to local qBit."""
    records: list[dict] = []
    with pytest.raises(AssertionError):
        send_to_seedbox(
            b"xxx",
            host="10.20.30.52",
            runner=_recording_runner(records),
            emit_audit=False,
        )
    # Guardrail fires BEFORE any scp invocation.
    assert records == []


def test_send_to_seedbox_rejects_forbidden_dest_via_watch_dir():
    """Even if host looks correct, a forbidden substring in the dest
    (e.g. operator typo pointing to ``10.20.30.52:8990``) must blow up.
    """
    records: list[dict] = []
    with pytest.raises(AssertionError):
        send_to_seedbox(
            b"xxx",
            host=SEEDBOX_HOST,
            watch_dir="/mirror/10.20.30.52:8990/watch/",
            runner=_recording_runner(records),
            emit_audit=False,
        )
    assert records == []


def test_send_to_seedbox_raises_on_scp_failure(tmp_path):
    records: list[dict] = []
    with pytest.raises(RuntimeError, match="scp"):
        send_to_seedbox(
            b"xxx",
            runner=_recording_runner(records, rc=255, stderr="permission denied"),
            audit_log=tmp_path / "events.jsonl",
        )
    assert len(records) == 1  # scp attempted once, then raised
    event = json.loads((tmp_path / "events.jsonl").read_text().strip())
    assert event["outcome"] == "scp_rc_255"


def test_send_to_seedbox_rejects_empty_bytes():
    with pytest.raises(ValueError):
        send_to_seedbox(b"", emit_audit=False)


def test_send_to_seedbox_via_rclone_stages_then_pushes():
    """Happy path: ssh-cat staging succeeds, rclone copyto succeeds."""
    calls: list[dict] = []

    def runner(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": {
            k: v for k, v in kwargs.items() if k != "input"
        }})
        return _FakeRunResult(returncode=0)

    dest = send_to_seedbox_via_rclone(
        b"torrent-bytes",
        bridge_ssh_host="cronqj@media",
        runner=runner,
        emit_audit=False,
        name_hint="ruin",
    )
    # Two ssh calls: stage, then rclone+rm.
    assert len(calls) == 2
    assert calls[0]["cmd"][0] == "ssh"
    assert "cat >" in calls[0]["cmd"][-1]
    assert calls[1]["cmd"][0] == "ssh"
    assert "rclone" in calls[1]["cmd"][-1]
    assert "copyto" in calls[1]["cmd"][-1]
    # The destination reported is in canopus's watch dir.
    assert dest.startswith("seedbox:/home/prometheansun/watch/qbittorrent/")
    assert dest.endswith(".torrent")
    assert "ruin" in dest


def test_send_to_seedbox_via_rclone_rejects_non_seedbox_remote():
    with pytest.raises(AssertionError):
        send_to_seedbox_via_rclone(
            b"xxx",
            rclone_remote="localqbit:/watch/",
            runner=lambda *a, **k: _FakeRunResult(0),
            emit_audit=False,
        )


def test_send_to_seedbox_via_rclone_rejects_wrong_watch_dir():
    with pytest.raises(AssertionError, match="watch/qbittorrent"):
        send_to_seedbox_via_rclone(
            b"xxx",
            rclone_remote="seedbox:/home/prometheansun/incoming/",
            runner=lambda *a, **k: _FakeRunResult(0),
            emit_audit=False,
        )


def test_send_to_seedbox_via_rclone_rejects_forbidden_in_remote():
    with pytest.raises(AssertionError):
        send_to_seedbox_via_rclone(
            b"xxx",
            rclone_remote="seedbox:/mirror/10.20.30.52:8990/watch/qbittorrent/",
            runner=lambda *a, **k: _FakeRunResult(0),
            emit_audit=False,
        )


def test_send_to_seedbox_via_rclone_raises_on_stage_failure():
    calls: list = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        # Fail on first (stage) call.
        if len(calls) == 1:
            return _FakeRunResult(returncode=255, stderr=b"ssh failed")
        return _FakeRunResult(0)

    with pytest.raises(RuntimeError, match="staging"):
        send_to_seedbox_via_rclone(b"xxx", runner=runner, emit_audit=False)
    # Push was not attempted because stage failed first.
    assert len(calls) == 1


def test_send_to_seedbox_via_rclone_raises_on_push_failure():
    calls: list = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeRunResult(returncode=0)
        return _FakeRunResult(returncode=3, stderr="rclone: permission denied")

    with pytest.raises(RuntimeError, match="rclone"):
        send_to_seedbox_via_rclone(b"xxx", runner=runner, emit_audit=False)
    assert len(calls) == 2


def test_send_to_seedbox_writes_to_given_local_path(tmp_path):
    records: list[dict] = []
    local = tmp_path / "staged" / "mam.torrent"
    send_to_seedbox(
        b"torrent-bytes",
        local_path=local,
        runner=_recording_runner(records),
        emit_audit=False,
    )
    # File was staged where the caller asked.
    assert local.exists()
    assert local.read_bytes() == b"torrent-bytes"
    # scp's source path matched.
    assert str(local) in records[0]["cmd"]
