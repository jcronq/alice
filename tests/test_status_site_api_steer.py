"""Tests for ``/api/steer`` — the Running-now sub-agent steering endpoint.

Design: ``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md`` §7.6.

The endpoint writes a ``type: status-steer`` surface into SURFACE_DIR;
Speaking's intake loop reads it and routes the message through
``SendMessage(to=task_id, ...)`` inside a turn (where the SDK is reachable).
Status-site itself stays stdlib-only.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from status_site import server as S


def _mk_cfg(tmp_path: Path) -> S.Config:
    """Minimal Config stand-in — mirrors _mk_cfg in test_status_site_woy_actions."""
    status = tmp_path / "status"
    status.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    lock = threading.Lock()

    class _C:
        status_dir = status
        threads_dir = status / "threads"
        surface_dir = surface
        key = "k"
        public_url = "http://localhost:8095"

    cfg = _C()
    cfg.lock = lock
    return cfg


def test_handle_steer_writes_surface(tmp_path):
    cfg = _mk_cfg(tmp_path)
    out = S.handle_steer(
        cfg, {"key": "k", "task_id": "task-1025", "text": "try the other branch"}
    )
    assert out["ok"] is True
    assert out["task_id"] == "task-1025"
    files = list(cfg.surface_dir.glob("*-status-steer-task-1025*.md"))
    assert len(files) == 1
    doc = files[0].read_text()
    assert "type: status-steer" in doc
    assert "task_id: task-1025" in doc
    assert "try the other branch" in doc


def test_handle_steer_bad_key_403(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_steer(cfg, {"key": "nope", "task_id": "task-1", "text": "x"})
    assert e.value.code == 403


def test_handle_steer_bad_task_id_shape_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    for bad in ("", "nonsense", "task-", "task-ab", "task 1025", "../etc/passwd"):
        with pytest.raises(S.ReplyError) as e:
            S.handle_steer(cfg, {"key": "k", "task_id": bad, "text": "x"})
        assert e.value.code == 400, f"{bad!r} should 400"


def test_handle_steer_empty_text_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_steer(cfg, {"key": "k", "task_id": "task-1", "text": "   "})
    assert e.value.code == 400


def test_handle_steer_overlong_text_413(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_steer(
            cfg,
            {
                "key": "k",
                "task_id": "task-1",
                "text": "x" * (S.SUBAGENT_STEER_MAX + 1),
            },
        )
    assert e.value.code == 413


def test_handle_steer_accepts_message_alias(tmp_path):
    """Caller may send ``message`` instead of ``text`` — same field."""
    cfg = _mk_cfg(tmp_path)
    out = S.handle_steer(
        cfg, {"key": "k", "task_id": "task-1", "message": "nudge"}
    )
    assert out["ok"] is True
    doc = (cfg.surface_dir / out["dropped"]).read_text()
    assert "nudge" in doc


def test_handle_steer_is_registered_on_post_route():
    """Route table in do_POST must include /api/steer so the handler fires."""
    # Grep the module source as a smoke test for the registration.
    src = Path(S.__file__).read_text()
    assert '"/api/steer": handle_steer' in src
