"""Attachments: ``ingest_attachments`` + chat-tab uploads (#614)."""

from __future__ import annotations

import base64
import json
import time

import pytest

from status_site.server import (
    MAX_PER_FILE,
    Config,
    ReplyError,
    handle_chat,
    ingest_attachments,
)

KEY = "att-key"
ALIAS = "/home/alice/alice-mind/inner/status-uploads"


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


@pytest.fixture
def cfg(tmp_path):
    return Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
        upload_dir_alias=ALIAS,
    )


def test_ingest_accepts_allowed_and_writes_safe_names(cfg):
    raw = [
        {"name": "../../shot.png", "data": b64(b"PNG")},
        {"name": "notes.txt", "data": b64(b"hello")},
        {"name": "doc.pdf", "data": b64(b"%PDF")},
        {"name": "a.json", "data": b64(b"{}")},
        {"name": "b.zip", "data": b64(b"PK")},
    ]
    files = ingest_attachments(cfg, raw, "sub")
    assert [f["name"] for f in files] == [
        "shot.png",
        "notes.txt",
        "doc.pdf",
        "a.json",
        "b.zip",
    ]
    stored = sorted((cfg.upload_dir / "sub").iterdir())
    assert len(stored) == 5
    assert all(".." not in p.name for p in stored)
    assert (cfg.upload_dir / "sub").stat().st_mode & 0o077 == 0
    for f in files:
        assert f["path"].startswith(f"{ALIAS}/sub/")
    assert files[0]["bytes"] == 3
    assert (cfg.upload_dir / "sub" / files[0]["path"].rsplit("/", 1)[1]).read_bytes() == b"PNG"


def test_ingest_empty_and_none(cfg):
    assert ingest_attachments(cfg, [], "s") == []
    assert ingest_attachments(cfg, None, "s") == []
    assert not cfg.upload_dir.exists()


def test_ingest_rejects_over_10mb(cfg):
    cfg.max_upload = 25 * 1024 * 1024
    big = b64(b"x" * (MAX_PER_FILE + 1))
    with pytest.raises(ReplyError) as e:
        ingest_attachments(cfg, [{"name": "a.png", "data": big}], "s")
    assert e.value.code == 413
    ok = b64(b"x" * MAX_PER_FILE)
    assert len(ingest_attachments(cfg, [{"name": "a.png", "data": ok}], "s")) == 1


def test_ingest_rejects_too_many(cfg):
    raw = [{"name": f"f{i}.txt", "data": "eA=="} for i in range(6)]
    with pytest.raises(ReplyError) as e:
        ingest_attachments(cfg, raw, "s")
    assert e.value.code == 413


@pytest.mark.parametrize("name", ["evil.exe", "movie.mp4", "noext", "weird.zzzq"])
def test_ingest_rejects_disallowed_mime(cfg, name):
    with pytest.raises(ReplyError) as e:
        ingest_attachments(cfg, [{"name": name, "data": "eA=="}], "s")
    assert e.value.code == 415


def test_ingest_rejects_bad_base64_and_shape(cfg):
    for raw in (
        [{"name": "a.png", "data": "!!notbase64"}],
        ["not-a-dict"],
        "not-a-list",
    ):
        with pytest.raises(ReplyError) as e:
            ingest_attachments(cfg, raw, "s")
        assert e.value.code == 400


def test_ingest_is_all_or_nothing(cfg):
    raw = [
        {"name": "ok.png", "data": "eA=="},
        {"name": "bad.exe", "data": "eA=="},
    ]
    with pytest.raises(ReplyError):
        ingest_attachments(cfg, raw, "s")
    assert not cfg.upload_dir.exists()


def test_handle_chat_persists_files_and_surface(cfg):
    out = handle_chat(
        cfg,
        {
            "key": KEY,
            "text": "look",
            "files": [{"name": "shot.png", "data": b64(b"PNG")}],
        },
    )
    assert out == {"ok": True}
    msg = json.loads(cfg.chat_log.read_text().splitlines()[-1])
    assert msg["text"] == "look" and msg["files"] == ["shot.png"]

    (surface,) = cfg.surface_dir.glob("*.md")
    doc = surface.read_text()
    assert "Attachments (Read them" in doc
    stored = next((cfg.upload_dir / "chat").rglob("*shot.png"))
    rel = stored.relative_to(cfg.upload_dir)
    assert f"{ALIAS}/{rel} (3 bytes)" in doc


def test_handle_chat_attachment_only_and_no_files(cfg):
    handle_chat(
        cfg, {"key": KEY, "text": "", "files": [{"name": "a.txt", "data": b64(b"hi")}]}
    )
    assert "(no text: attachment only)" in next(cfg.surface_dir.glob("*.md")).read_text()
    with pytest.raises(ReplyError) as e:
        handle_chat(cfg, {"key": KEY, "text": "  ", "files": []})
    assert e.value.code == 400


def test_handle_chat_text_only_has_empty_files(cfg):
    handle_chat(cfg, {"key": KEY, "text": "hi"})
    msg = json.loads(cfg.chat_log.read_text().splitlines()[-1])
    assert msg["files"] == []
    assert "  none" in next(cfg.surface_dir.glob("*.md")).read_text()


def test_handle_chat_rejects_bad_file_without_logging(cfg):
    with pytest.raises(ReplyError) as e:
        handle_chat(
            cfg, {"key": KEY, "text": "x", "files": [{"name": "a.exe", "data": "eA=="}]}
        )
    assert e.value.code == 415
    assert not cfg.chat_log.exists()


def test_chat_message_record_carries_attachment_paths(cfg):
    """GET /api/chat serves ``read_chat``; the record must expose the stored alias (#655)."""
    from status_site.server import read_chat

    handle_chat(
        cfg,
        {
            "key": KEY,
            "text": "look",
            "files": [{"name": "shot.png", "data": b64(b"PNG")}],
        },
    )
    (msg,) = read_chat(cfg)
    (att,) = msg["attachments"]
    stored = next((cfg.upload_dir / "chat").rglob("*shot.png"))
    assert stored.parent.name == time.strftime("%Y-%m-%d")
    assert stored.parent.parent.name == "chat"
    assert att == {
        "name": "shot.png",
        "path": f"{ALIAS}/{stored.relative_to(cfg.upload_dir)}",
        "bytes": 3,
    }
