"""Tests for :mod:`alice_mam.deliver` — wait, extract, calibre, SMTP.

No live network / ssh / docker. All external surfaces (sleep, runner,
copyfile) are injected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice_mam.deliver import (
    CalibreAddError,
    KindleSendError,
    _host_path_to_container,
    _lister_over_ssh,
    _name_matches_hint,
    _parse_book_id,
    calibre_add,
    extract_epub,
    extract_epub_via_ssh,
    send_to_kindle,
    wait_for_download,
)


# ─── wait_for_download ─────────────────────────────────────────────────


def test_wait_for_download_matches_immediately(tmp_path):
    (tmp_path / "Twice_Dead_King_Ruin_Nate_Crowley").mkdir()
    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=tmp_path,
        timeout_minutes=1,
        poll_interval_seconds=1,
        sleep=lambda _s: None,
    )
    assert out.name == "Twice_Dead_King_Ruin_Nate_Crowley"


def test_wait_for_download_matches_scene_style_name(tmp_path):
    """Scene-style names (dots, brackets) still token-overlap."""
    (tmp_path / "[BlackLibrary]_Twice.Dead.King-Ruin.2026").mkdir()
    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=tmp_path,
        timeout_minutes=1,
        poll_interval_seconds=1,
        sleep=lambda _s: None,
    )
    assert out.name.startswith("[BlackLibrary]")


def test_wait_for_download_ignores_unrelated_dirs(tmp_path):
    """A download for a different book must not satisfy the wait."""
    (tmp_path / "Completely_Unrelated_Book").mkdir()
    fake_time = {"now": 0.0}

    def now():
        return fake_time["now"]

    def sleep(s):
        fake_time["now"] += s

    with pytest.raises(TimeoutError):
        wait_for_download(
            "Twice Dead King Ruin",
            downloads_dir=tmp_path,
            timeout_minutes=1,
            poll_interval_seconds=30,
            sleep=sleep,
            now=now,
        )


def test_wait_for_download_times_out(tmp_path):
    fake_time = {"now": 0.0}

    def now():
        return fake_time["now"]

    def sleep(s):
        fake_time["now"] += s

    with pytest.raises(TimeoutError):
        wait_for_download(
            "Nonexistent Book",
            downloads_dir=tmp_path,
            timeout_minutes=1,
            poll_interval_seconds=30,
            sleep=sleep,
            now=now,
        )


def test_wait_for_download_eventually_matches(tmp_path):
    """Dir appears on the 3rd poll — must still match."""
    fake_time = {"now": 0.0, "polls": 0}

    def now():
        return fake_time["now"]

    def sleep(s):
        fake_time["now"] += s
        fake_time["polls"] += 1
        if fake_time["polls"] == 3:
            (tmp_path / "Twice_Dead_King_Ruin").mkdir()

    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=tmp_path,
        timeout_minutes=10,
        poll_interval_seconds=30,
        sleep=sleep,
        now=now,
    )
    assert out.name == "Twice_Dead_King_Ruin"


def test_wait_for_download_matches_single_file(tmp_path):
    """MAM epub-only torrents often land as a single .epub file."""
    (tmp_path / "Nate Crowley - The Twice-dead King 01 - Ruin.epub").write_bytes(b"EPUB")
    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=tmp_path,
        timeout_minutes=1,
        poll_interval_seconds=1,
        sleep=lambda _s: None,
    )
    assert out.is_file()
    assert out.name.endswith(".epub")


def test_wait_for_download_prefers_dir_over_file(tmp_path):
    """When both a matching dir AND a matching file exist, dir wins."""
    (tmp_path / "Ruin_scan.epub").write_bytes(b"BAD")
    (tmp_path / "Twice_Dead_King_Ruin").mkdir()
    (tmp_path / "Twice_Dead_King_Ruin" / "book.epub").write_bytes(b"GOOD")
    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=tmp_path,
        timeout_minutes=1,
        poll_interval_seconds=1,
        sleep=lambda _s: None,
    )
    assert out.is_dir()


def test_extract_epub_accepts_single_file(tmp_path):
    """When wait_for_download returned a .epub file directly."""
    src = tmp_path / "book.epub"
    src.write_bytes(b"EPUB")
    dest = tmp_path / "flat"
    out = extract_epub(src, dest=dest)
    assert out == dest / "book.epub"
    assert out.read_bytes() == b"EPUB"


def test_extract_epub_rejects_non_epub_file(tmp_path):
    src = tmp_path / "cover.jpg"
    src.write_bytes(b"JPG")
    with pytest.raises(FileNotFoundError):
        extract_epub(src, dest=tmp_path / "flat")


def test_name_matches_hint_token_overlap():
    assert _name_matches_hint("Twice.Dead.King-Ruin", "Twice Dead King Ruin")
    assert not _name_matches_hint("Dune_Herbert", "Twice Dead King Ruin")


# ─── extract_epub ──────────────────────────────────────────────────────


def test_extract_epub_picks_first_epub(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    # Create two epubs in subdirs; the alphabetically-first wins.
    (dl / "sub").mkdir()
    (dl / "sub" / "beta.epub").write_bytes(b"BETA")
    (dl / "alpha.epub").write_bytes(b"ALPHA")

    dest = tmp_path / "flat"
    out = extract_epub(dl, dest=dest)
    assert out.name == "alpha.epub"
    assert out.read_bytes() == b"ALPHA"


def test_extract_epub_raises_when_no_epub(tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "cover.jpg").write_bytes(b"IMG")
    with pytest.raises(FileNotFoundError):
        extract_epub(dl, dest=tmp_path / "flat")


def test_extract_epub_raises_when_download_dir_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_epub(tmp_path / "nope", dest=tmp_path / "flat")


# ─── calibre_add ───────────────────────────────────────────────────────


def test_host_path_to_container_maps_flatbooks():
    assert (
        _host_path_to_container(
            Path("/home/cronqj/media/flatbooks/Ruin.epub")
        )
        == "/flatbooks/Ruin.epub"
    )


def test_host_path_to_container_passes_through_other_paths():
    # Not a hard error; the remote side will report file-not-found if
    # the path isn't mounted. We just preserve the string.
    other = Path("/tmp/weird/book.epub")
    assert _host_path_to_container(other) == str(other)


def test_parse_book_id_extracts_first():
    assert _parse_book_id("Added book ids: 42") == 42
    assert _parse_book_id("Added book ids: 100, 101") == 100
    assert _parse_book_id("noise\nAdded book ids: 7\nmore noise") == 7


def test_parse_book_id_raises_on_missing_marker():
    with pytest.raises(CalibreAddError):
        _parse_book_id("some unrelated output")


class _FakeRunResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_calibre_add_invokes_ssh_docker_exec():
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeRunResult(stdout="Added book ids: 99\n")

    book_id = calibre_add(
        Path("/home/cronqj/media/flatbooks/Twice Dead King Ruin.epub"),
        runner=runner,
    )
    assert book_id == 99
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "ssh"
    assert cmd[-2] == "cronqj@10.20.30.52"
    remote = cmd[-1]
    assert "docker exec calibre-web calibredb add" in remote
    assert "--library-path=/books" in remote
    # The container-path translation happened.
    assert "/flatbooks/Twice Dead King Ruin.epub" in remote
    # And we did NOT send the host path.
    assert "/home/cronqj/media/flatbooks/" not in remote


def test_calibre_add_raises_on_non_zero_exit():
    def runner(cmd, **kwargs):
        return _FakeRunResult(returncode=2, stderr="no such file")

    with pytest.raises(CalibreAddError):
        calibre_add(
            Path("/home/cronqj/media/flatbooks/ghost.epub"),
            runner=runner,
        )


def test_calibre_add_raises_on_unparseable_stdout():
    def runner(cmd, **kwargs):
        return _FakeRunResult(stdout="nothing mentioning a book id")

    with pytest.raises(CalibreAddError):
        calibre_add(
            Path("/home/cronqj/media/flatbooks/x.epub"),
            runner=runner,
        )


# ─── send_to_kindle ────────────────────────────────────────────────────


def test_send_to_kindle_parses_remote_json():
    status_payload = {
        "ok": True,
        "kindle_email": "jason_xxxx@kindle.com",
        "server": "smtp.gmail.com",
        "port": 587,
        "title": "Ruin",
        "epub_path": "/books/.../Ruin.epub",
    }
    captured: dict = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input")
        return _FakeRunResult(stdout=json.dumps(status_payload))

    out = send_to_kindle(42, runner=runner)
    assert out["ok"] is True
    assert out["kindle_email"] == "jason_xxxx@kindle.com"
    cmd = captured["cmd"]
    assert cmd[0] == "ssh"
    # BOOK_ID forwarded.
    assert "BOOK_ID=42" in cmd[-1]
    # The embedded script was piped on stdin.
    assert captured["input"] is not None
    assert "fernet.decrypt" in captured["input"]


def test_send_to_kindle_forwards_kindle_email_override():
    captured: dict = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeRunResult(stdout=json.dumps({"ok": True, "kindle_email": "x@kindle.com"}))

    send_to_kindle(42, kindle_email="x@kindle.com", runner=runner)
    assert "KINDLE_EMAIL=x@kindle.com" in captured["cmd"][-1]


def test_send_to_kindle_raises_when_remote_reports_failure():
    def runner(cmd, **kwargs):
        return _FakeRunResult(
            stdout=json.dumps({"ok": False, "stage": "smtp", "err": "535 bad auth"})
        )

    with pytest.raises(KindleSendError, match="stage=smtp"):
        send_to_kindle(42, runner=runner)


def test_send_to_kindle_raises_when_no_json_in_stdout():
    def runner(cmd, **kwargs):
        return _FakeRunResult(stdout="some ssh banner\nno json", returncode=1, stderr="err")

    with pytest.raises(KindleSendError, match="no JSON status"):
        send_to_kindle(42, runner=runner)


def test_send_to_kindle_rejects_bad_book_id():
    with pytest.raises(ValueError):
        send_to_kindle(0)
    with pytest.raises(ValueError):
        send_to_kindle(-1)


def test_lister_over_ssh_parses_find_output():
    """`find -printf '%y|%f\\n'` lines turn into (name, is_dir) tuples."""
    def runner(cmd, **kwargs):
        return _FakeRunResult(
            stdout="d|Twice Dead King Ruin\nf|cover.jpg\nd|Other Book\n"
        )

    lister = _lister_over_ssh("media", runner=runner)
    rows = lister(Path("/mnt/plex/downloads/seedbox"))
    assert ("Twice Dead King Ruin", True) in rows
    assert ("cover.jpg", False) in rows
    assert ("Other Book", True) in rows


def test_wait_for_download_with_remote_lister():
    calls: list = []
    def runner(cmd, **kwargs):
        calls.append(cmd)
        return _FakeRunResult(stdout="f|Nate_Crowley_Twice_Dead_King_Ruin.epub\n")

    lister = _lister_over_ssh("media", runner=runner)
    out = wait_for_download(
        "Twice Dead King Ruin",
        downloads_dir=Path("/mnt/plex/downloads/seedbox"),
        timeout_minutes=1,
        poll_interval_seconds=1,
        sleep=lambda _s: None,
        lister=lister,
    )
    # Remote match composes the remote absolute path on the alice side.
    assert str(out).endswith(".epub")
    assert len(calls) == 1
    # And we invoked find with -printf through ssh.
    assert "find" in calls[0][-1]


def test_extract_epub_via_ssh_returns_remote_path():
    def runner(cmd, **kwargs):
        return _FakeRunResult(stdout="/home/cronqj/media/flatbooks/book.epub\n")

    out = extract_epub_via_ssh(
        Path("/mnt/plex/downloads/seedbox/some.epub"),
        ssh_host="cronqj@media",
        runner=runner,
    )
    assert str(out) == "/home/cronqj/media/flatbooks/book.epub"


def test_extract_epub_via_ssh_raises_on_empty_output():
    def runner(cmd, **kwargs):
        return _FakeRunResult(returncode=2, stderr="no .epub")

    with pytest.raises(FileNotFoundError):
        extract_epub_via_ssh(
            Path("/mnt/plex/downloads/seedbox/nope"),
            runner=runner,
        )


def test_send_to_kindle_tolerates_json_with_leading_noise():
    """calibre tooling sometimes leaks warnings above the JSON line."""
    status_payload = {"ok": True, "kindle_email": "j@kindle.com"}

    def runner(cmd, **kwargs):
        stdout = (
            "DeprecationWarning: calibre thing\n"
            "another warning line\n"
            + json.dumps(status_payload)
        )
        return _FakeRunResult(stdout=stdout)

    out = send_to_kindle(42, runner=runner)
    assert out["kindle_email"] == "j@kindle.com"
