"""Log rotation: the viewer's tail readers survive rotation, and the
logrotate policy covers every runtime log with the right mode.

Rotation styles in sandbox/logrotate/alice-logs.conf:
- rename + create (event logs written open-append-close per line)
- copytruncate (stderr logs held open by long-lived writers)

Both must reset the viewer's cached offsets rather than seek a stale
offset into a different/shorter file and emit half-lines.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from viewer import main as viewer_main
from viewer import sources

REPO = pathlib.Path(__file__).resolve().parents[1]
CONF = REPO / "sandbox" / "logrotate" / "alice-logs.conf"


@pytest.fixture(autouse=True)
def _clear_cache():
    sources._jsonl_caches.clear()
    yield
    sources._jsonl_caches.clear()


def _line(i: int, pad: int = 50) -> bytes:
    return (
        json.dumps({"ts": float(i), "event": "assistant", "i": i, "t": "x" * pad})
        + "\n"
    ).encode()


def _append(path: pathlib.Path, start: int, n: int, pad: int = 50) -> None:
    with path.open("ab") as f:
        f.write(b"".join(_line(i, pad) for i in range(start, start + n)))


def _ids(events) -> list[int]:
    return [e.detail["i"] for e in events]


# ---------------------------------------------------------------------------
# _cached_jsonl (read_thinking / read_speaking)


def test_cache_resets_on_rename_rotation_even_if_new_file_is_larger(tmp_path):
    """Size-only detection misses a recreated file that already outgrew
    the old offset; inode tracking must catch it."""
    log = tmp_path / "thinking.log"
    _append(log, 0, 10)
    assert _ids(sources.read_thinking(log)) == list(range(10))

    log.rename(tmp_path / "thinking.log.1")
    _append(log, 100, 40, pad=80)  # bigger than the rotated file
    assert log.stat().st_size > (tmp_path / "thinking.log.1").stat().st_size

    assert _ids(sources.read_thinking(log)) == list(range(100, 140))


def test_cache_resets_on_copytruncate(tmp_path):
    log = tmp_path / "speaking.log"
    _append(log, 0, 20)
    assert len(sources.read_thinking(log)) == 20

    shutil.copyfile(log, tmp_path / "speaking.log.1")
    with log.open("r+b") as f:
        f.truncate(0)
    _append(log, 500, 3)

    assert _ids(sources.read_thinking(log)) == [500, 501, 502]


def test_cache_handles_rotated_path_briefly_missing(tmp_path):
    log = tmp_path / "thinking.log"
    _append(log, 0, 5)
    sources.read_thinking(log)
    log.rename(tmp_path / "thinking.log.1")
    assert sources.read_thinking(log) == []
    _append(log, 7, 2)
    assert _ids(sources.read_thinking(log)) == [7, 8]


# ---------------------------------------------------------------------------
# /stream helper


def _decoded(lines: list[str]) -> list[int]:
    return [json.loads(x)["i"] for x in lines]


def test_read_since_follows_rename_rotation(tmp_path):
    log = tmp_path / "thinking.log"
    _append(log, 0, 3)
    cur = viewer_main._eof_cursor(log)
    _append(log, 3, 2)
    lines, cur = viewer_main._read_since(log, cur)
    assert _decoded(lines) == [3, 4]

    log.rename(tmp_path / "thinking.log.1")
    _append(log, 10, 30, pad=200)  # larger than old offset
    lines, cur = viewer_main._read_since(log, cur)
    assert _decoded(lines) == list(range(10, 40))
    assert viewer_main._read_since(log, cur)[0] == []


def test_read_since_follows_copytruncate(tmp_path):
    log = tmp_path / "speaking.log"
    _append(log, 0, 10)
    cur = viewer_main._eof_cursor(log)
    with log.open("r+b") as f:
        f.truncate(0)
    _append(log, 50, 2)
    lines, cur = viewer_main._read_since(log, cur)
    assert _decoded(lines) == [50, 51]


def test_read_since_holds_back_partial_line(tmp_path):
    log = tmp_path / "thinking.log"
    log.write_bytes(b"")
    cur = viewer_main._eof_cursor(log)
    full = _line(1)
    with log.open("ab") as f:
        f.write(_line(0) + full[:10])
    lines, cur = viewer_main._read_since(log, cur)
    assert _decoded(lines) == [0]
    with log.open("ab") as f:
        f.write(full[10:])
    lines, cur = viewer_main._read_since(log, cur)
    assert _decoded(lines) == [1]


def test_read_since_missing_file(tmp_path):
    log = tmp_path / "nope.log"
    cur = viewer_main._eof_cursor(log)
    assert viewer_main._read_since(log, cur) == ([], cur)
    _append(log, 0, 1)
    assert _decoded(viewer_main._read_since(log, cur)[0]) == [0]


# ---------------------------------------------------------------------------
# logrotate policy


def _stanzas(text: str) -> dict[str, str]:
    """Map each path pattern to the body of the stanza it belongs to."""
    text = re.sub(r"#.*", "", text)
    out: dict[str, str] = {}
    for head, body in re.findall(r"([^{}]*)\{([^{}]*)\}", text):
        for tok in head.split():
            if tok.startswith("/"):
                out[tok] = body
    return out


def test_policy_rotation_mode_matches_writer():
    stanzas = _stanzas(CONF.read_text())
    # Open-append-close-per-event writers → rename is lossless.
    for p in ("thinking.log", "speaking.log", "memory-worker.log"):
        body = stanzas[f"/state/worker/{p}"]
        assert "copytruncate" not in body
        assert "delaycompress" in body
    # Long-held fds (redirfd -a 2, signal-cli >>) → must copytruncate.
    for p in ("/state/worker/*-stderr.log", "/state/daemon/signal-daemon.log"):
        assert "copytruncate" in stanzas[p]
    for body in stanzas.values():
        assert re.search(r"\bsize 256M\b", body)
        assert re.search(r"\brotate 5\b", body)
        assert "compress" in body


def test_s6_service_wired_into_image():
    dockerfile = (REPO / "sandbox" / "Dockerfile").read_text()
    assert "sandbox/s6/alice-logrotate/" in dockerfile
    assert "contents.d/alice-logrotate" in dockerfile
    assert "/etc/alice/logrotate.conf" in dockerfile
    assert (REPO / "sandbox/s6/user/contents.d/alice-logrotate").exists()
    assert (REPO / "sandbox/s6/alice-logrotate/type").read_text().strip() == "longrun"


@pytest.mark.skipif(shutil.which("logrotate") is None, reason="logrotate not installed")
def test_policy_rotates_for_real(tmp_path):
    """Run the shipped policy (paths retargeted, `su` dropped) end to end."""
    state = tmp_path / "state"
    (state / "worker").mkdir(parents=True)
    (state / "daemon").mkdir()
    conf_text = CONF.read_text().replace("/state/", f"{state}/")
    conf_text = re.sub(r"^su .*$", "", conf_text, flags=re.M)
    conf_text = re.sub(r"^\s*create .*$", "    create", conf_text, flags=re.M)
    conf_text = conf_text.replace("size 256M", "size 1k")
    conf = tmp_path / "rot.conf"
    conf.write_text(conf_text)
    conf.chmod(0o644)

    thinking = state / "worker" / "thinking.log"
    stderr = state / "worker" / "speaking-stderr.log"
    _append(thinking, 0, 100)
    _append(stderr, 0, 100)
    held = stderr.open("ab")  # simulate redirfd's long-lived O_APPEND fd
    try:
        subprocess.run(
            ["logrotate", "-s", str(tmp_path / "status"), str(conf)],
            check=True,
            capture_output=True,
        )
        held.write(_line(999))
        held.flush()
    finally:
        held.close()

    assert (state / "worker" / "thinking.log.1").exists()
    assert thinking.exists() and thinking.stat().st_size == 0
    assert (state / "worker" / "speaking-stderr.log.1.gz").exists()
    # Held fd keeps writing into the live (truncated) file, at offset 0.
    assert stderr.read_bytes() == _line(999)


def test_read_since_bounds_replay_into_large_rotated_file(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_main, "_STREAM_ROTATION_REPLAY_BYTES", 1000)
    log = tmp_path / "thinking.log"
    _append(log, 0, 3)
    cur = viewer_main._eof_cursor(log)
    log.rename(tmp_path / "thinking.log.1")
    _append(log, 0, 500)  # ~40 KB, far past the replay bound
    lines, cur = viewer_main._read_since(log, cur)
    ids = _decoded(lines)
    assert 0 < len(ids) < 20
    assert ids == list(range(ids[0], 500))  # whole lines, ending at the tail
